"""``Peer``: one remote party on asyncio, over ``PeerState``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from rulfo._state import PeerState
from rulfo._transport import Transport
from rulfo._types import (
    Busy,
    Closed,
    Config,
    Ended,
    Event,
    Outcome,
    Overloaded,
    PeerClosed,
    ProtocolError,
    Ready,
    Reply,
    ReplyReceived,
    RequestReceived,
    Result,
    RulfoError,
)

_log = logging.getLogger("rulfo")

_ID_SIZE = 16
# The most one call of Transport.send gets. Some transports refuse large messages: the
# websockets library closes a connection above 1 MiB.
_PIECE = 64 * 1024
_MAX_KEY = 0xFFFF
_LONGEST_TIMEOUT = 10**9  # seconds
# The loop's timers stop while the machine is suspended and the clock of rulfo does not, so no
# wait is longer than this.
_LONGEST_WAIT_MS = 1000


def check_request(key: str, body: bytes, max_length: int) -> None:
    """Raises ValueError for a key and body that cannot be sent."""
    size = len(key.encode())
    if not 0 < size <= _MAX_KEY or size + len(body) > max_length:
        msg = f"a key of {size} bytes with a body of {len(body)} cannot be sent"
        raise ValueError(msg)


def timeout_ms(timeout: float) -> int:
    """``timeout`` in milliseconds. Raises ValueError outside the range."""
    if not 0 < timeout <= _LONGEST_TIMEOUT:
        msg = f"timeout must be above 0 and at most {_LONGEST_TIMEOUT} seconds"
        raise ValueError(msg)
    return max(1, round(timeout * 1000))


def _boot_clock() -> int:
    """Milliseconds that never go back and count the time the machine is suspended."""
    if sys.platform == "darwin":  # on macOS this one goes on while the machine sleeps
        clock = time.CLOCK_MONOTONIC_RAW
    else:
        clock = time.CLOCK_BOOTTIME
    return time.clock_gettime_ns(clock) // 1_000_000


@dataclass(eq=False)
class _Link:
    """One connection, from ``serve``."""

    transport: Transport
    wake: asyncio.Event = field(default_factory=asyncio.Event)  # bytes wait to be sent
    stop: asyncio.Event = field(default_factory=asyncio.Event)  # end this connection
    done: asyncio.Event = field(default_factory=asyncio.Event)  # nothing more is read from it
    sending: int = 0  # bytes given to the transport and not yet sent
    error: RulfoError | None = None  # why this side ends the connection


@dataclass
class _Run:
    """What one call of a handler has done."""

    replies: int = 0
    done: bool = False


@dataclass
class _Pending:
    """A call of ``request`` that waits for its outcome."""

    future: asyncio.Future[Result]
    replies: list[Reply] = field(default_factory=list[Reply])


class Request:
    """A request that a handler runs.

    Attributes:
        key: The key this request was sent to.
        body: The bytes the requester sent.
        id: The 16-byte id chosen by the requester. The same across retries.
        caller: The ``name`` of the ``Peer`` it came to. With ``Server``, the authenticated
            name given to ``Server.serve``.
        repeat: True when the requester sent this id before and this peer does not know it.
            A process that ran earlier may have run the request. A handler that must not run
            twice across a restart checks its own record when ``repeat`` is true.
    """

    def __init__(
        self,
        event: RequestReceived,
        remaining: Callable[[], float],
        send: Callable[[bytes, bool], Awaitable[None]],
        caller: str,
    ) -> None:
        self.caller = caller
        self.id = event.id
        self.key = event.key
        self.body = event.body
        self.repeat = event.repeat
        self._remaining = remaining
        self._send = send

    def remaining(self) -> float:
        """Seconds until the deadline, 0 after it. The handler is cancelled then."""
        return self._remaining()

    async def reply(self, body: bytes, *, error: bool = False) -> None:
        """Sends one reply. Waits while too many bytes are not yet sent.

        Without a connection the reply is stored and sent when the requester asks again.

        Raises:
            ValueError: A body longer than ``max_length``.
            RuntimeError: The handler of this request has finished.
        """
        await self._send(body, error)


Handler = Callable[[Request], Awaitable[bytes | None]]


class Peer:
    """The requests to and from one remote party, over one connection after another.

    Keep one ``Peer`` for each remote party, also across reconnects: it remembers the ids it ran.
    A second ``Peer`` for the same party runs a repeated request again. For many clients, use
    ``Server``, which keeps one ``Peer`` per authenticated name and drops idle ones.

    Use it from one event loop.

    Args:
        handlers: The handler for each key this side serves. It returns the one reply, or None
            after it sent its replies with ``Request.reply``. It raises ``Busy`` to refuse.
        config: The settings.
        clock: Milliseconds that never go back and count suspended time. Processes that send
            copies of one id need a shared clock (rule 14 in the operations page).
        name: A name for the remote party. Handlers read it as ``Request.caller``. With
            ``Server``, this is the authenticated name passed to ``Server.serve``.
    """

    def __init__(
        self,
        *,
        handlers: Mapping[str, Handler] | None = None,
        config: Config | None = None,
        clock: Callable[[], int] | None = None,
        name: str = "",
    ) -> None:
        self.name = name
        self._calls = 0  # calls of request and request_copy that have not returned
        self._state = PeerState(config)
        self.config = self._state.config
        self._handlers = dict(handlers or {})
        self._clock = clock or _boot_clock
        self._link: _Link | None = None
        self._replace = asyncio.Lock()
        self._changed = asyncio.Event()  # set, and replaced, when a waiting call must look again
        self._pending: dict[bytes, _Pending] = {}
        self._running: set[asyncio.Task[None]] = set()
        self._timer: asyncio.TimerHandle | None = None
        self._closed = False

    @property
    def connected(self) -> bool:
        """Whether a request can be sent now."""
        return self._link is not None and self._state.ready

    @property
    def running(self) -> int:
        """Handlers that run now."""
        return len(self._running)

    @property
    def idle(self) -> bool:
        """No open request, running handler or kept id: dropping the ``Peer`` loses nothing."""
        return self._state.idle and not self._running and not self._calls

    async def wait_connected(self) -> None:
        """Returns when a request can be sent. Limit the wait with ``asyncio.timeout``.

        Raises:
            PeerClosed: ``aclose`` was called.
        """
        while not self.connected:
            if self._closed:
                raise PeerClosed
            await self._changed.wait()

    async def serve(self, transport: Transport) -> None:
        """Uses one connection until it ends, and closes it.

        A call replaces the connection of an earlier call, which then returns. The caller decides
        when to connect again.

        Raises:
            PeerClosed: ``serve`` was called after ``aclose``. A call that runs when ``aclose``
                is called returns.
            ProtocolError: The other side broke the wire format, or the handshake took too long.
            Overloaded: The other side takes too little of what this side sends.
            Exception: What ``transport`` raises.
        """
        try:
            link = await self._start_link(transport)
        except BaseException:
            await transport.close()
            raise
        tasks: list[asyncio.Task[object]] = [
            asyncio.create_task(self._read(link)),
            asyncio.create_task(self._write(link)),
            asyncio.create_task(link.stop.wait()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if link.error is not None:
                raise link.error
            if not link.stop.is_set():
                for task in done:
                    task.result()
        finally:
            for task in tasks:
                task.cancel()
            # Before the first await: a second cancel must not leave the connection in place.
            self._drop(link)
            link.done.set()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                await transport.close()

    async def _start_link(self, transport: Transport) -> _Link:
        """Ends the connection in place, and starts one on ``transport``."""
        async with self._replace:
            await self._end_link()
            if self._closed:
                raise PeerClosed
            link = _Link(transport)
            self._link = link
            self._state.connected(self._clock())
            self._after()
        return link

    async def request(
        self,
        key: str,
        body: bytes = b"",
        *,
        timeout: float,  # noqa: ASYNC109 -- the deadline that goes on the wire
    ) -> Result:
        """Sends a request and returns its outcome with the replies.

        Without a connection the outcome is ``unroutable`` at once. A request that was sent
        stays open across reconnects until ``timeout``. Cancelling the call stops the wait: the
        other side is not told, and may run the request to its end.

        Args:
            key: Which handler runs it.
            body: The bytes for the handler.
            timeout: Seconds until the request expires. All replies are kept in memory.

        Raises:
            ValueError: An empty key, or a key and body above the largest length.
            PeerClosed: ``aclose`` was called.
        """
        check_request(key, body, self.config.max_length)
        deadline = self._clock() + timeout_ms(timeout)
        return await self._call(os.urandom(_ID_SIZE), key, body, deadline, sent_before=False)

    async def request_copy(
        self,
        request_id: bytes,
        key: str,
        body: bytes = b"",
        *,
        deadline_ms: int,
        sent_before: bool,
    ) -> Result:
        """Sends a request whose id and deadline the caller chose, and returns its outcome.

        Use it when more than one process may send the same request, for example when a second
        process takes over from one that stopped. All of them pass the same id, key, body and
        deadline, and read the same clock (rule 14 in the operations page).

        With ``sent_before`` false it behaves as ``request``. With ``sent_before`` true another
        copy may have run, so the outcome is never ``unroutable`` or ``busy``: without a
        connection the call waits for one. The replier runs the request only if it does not know
        the id. A replier that cannot take it (no handler for the key, or a limit reached) does
        not answer, and the call ends ``expired`` at the deadline.

        Do not send an id again after you cancelled its call.

        Args:
            request_id: 16 random bytes, the same in every copy.
            key: Which handler runs it.
            body: The bytes for the handler.
            deadline_ms: When the request expires, in milliseconds on ``clock``.
            sent_before: Whether a copy may have been sent already. When in doubt, pass true.

        Raises:
            ValueError: An id that is not 16 bytes, a deadline too far away, an empty key, or a
                key and body above the largest length.
            CallError: This ``Peer`` already waits for this id, or ``sent_before`` is true and
                the other side does not drop repeated ids.
            PeerClosed: ``aclose`` was called.
        """
        if len(request_id) != _ID_SIZE:
            msg = f"a request id has {_ID_SIZE} bytes, not {len(request_id)}"
            raise ValueError(msg)
        check_request(key, body, self.config.max_length)
        if deadline_ms - self._clock() > _LONGEST_TIMEOUT * 1000:
            msg = f"the deadline is more than {_LONGEST_TIMEOUT} seconds away"
            raise ValueError(msg)
        return await self._call(request_id, key, body, deadline_ms, sent_before=sent_before)

    async def _call(
        self, request_id: bytes, key: str, body: bytes, deadline: int, *, sent_before: bool
    ) -> Result:
        """Waits until the request can be sent, sends it and waits for its outcome."""
        self._calls += 1
        try:
            while True:
                if self._closed:
                    raise PeerClosed
                now = self._clock()
                if now >= deadline:
                    return Result(Outcome.EXPIRED, ())
                if self._link is None and not sent_before:
                    return Result(Outcome.UNROUTABLE, ())
                if (
                    self._link is not None
                    and self._state.ready
                    and self._state.open_requests < self.config.max_requests
                    and self._buffered() <= self.config.send_buffer
                ):
                    break
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(min(deadline - now, _LONGEST_WAIT_MS) / 1000):
                        await self._changed.wait()
            self._state.request(request_id, key, body, deadline, now, sent_before=sent_before)
            pending = _Pending(asyncio.get_running_loop().create_future())
            self._pending[request_id] = pending
            self._after()
            try:
                return await pending.future
            finally:
                if self._pending.pop(request_id, None) is not None:  # cancelled
                    self._state.abandon(request_id)
                    self._after()
                    self._notify()
        finally:
            self._calls -= 1

    async def aclose(self) -> None:
        """Closes the connection, fails the open requests and cancels the handlers.

        Bytes that are not sent yet are dropped, also the end of a handler that just returned.
        """
        await self._wait_closed(self._close_now())

    def _close_now(self) -> tuple[_Link | None, list[asyncio.Task[None]]]:
        """The part of ``aclose`` that does not wait. Returns what ``_wait_closed`` waits for."""
        # Everything is done here, so a cancelled ``aclose`` leaves nothing waiting.
        self._closed = True
        link = self._link
        if link is not None:
            # The connection goes first: a cancelled handler must not send an end.
            link.stop.set()
            self._drop(link)
        pending, self._pending = self._pending, {}
        for request_id, waiting in pending.items():
            self._state.abandon(request_id)
            if not waiting.future.done():
                waiting.future.set_exception(PeerClosed())
        handlers = [task for task in self._running if task is not asyncio.current_task()]
        for task in handlers:
            task.cancel()
        self._arm()
        self._notify()
        return link, handlers

    async def _wait_closed(self, closing: tuple[_Link | None, list[asyncio.Task[None]]]) -> None:
        link, handlers = closing
        if link is not None:
            await link.done.wait()
        await asyncio.gather(*handlers, return_exceptions=True)

    def _buffered(self) -> int:
        return self._state.unsent + (self._link.sending if self._link else 0)

    async def _drain(self) -> None:
        """Waits while the connection has too many bytes to send."""
        while self._link is not None and self._buffered() > self.config.send_buffer:
            await self._changed.wait()

    def _notify(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def _unsent_limit(self) -> int:
        """More unsent bytes than this end the connection.

        ``request`` and ``reply`` wait far below it. The refusals, the requests sent again and
        the replies sent again do not wait: a request sent again and the stored replies are
        counted here, and refusals reach the limit only when the other side stops reading.
        """
        config = self.config
        return (
            4 * config.send_buffer
            + config.max_length
            + config.replay_bytes
            + self._state.open_bytes
        )

    def _after(self) -> None:
        """Runs after every call of the state: bytes may wait and the next time may differ."""
        link = self._link
        if link is not None and not link.stop.is_set():
            if self._buffered() > self._unsent_limit():
                link.error = Overloaded("the other side does not take what is sent to it")
                link.stop.set()
            elif self._state.unsent:
                link.wake.set()
        self._arm()

    def _arm(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        due = self._state.next_time()
        if due is None or self._closed:
            return
        # At least 1 ms: a timer that fires a little early must not spin.
        wait = min(max(due - self._clock(), 1), _LONGEST_WAIT_MS)
        self._timer = asyncio.get_running_loop().call_later(wait / 1000, self._on_timer)

    def _on_timer(self) -> None:
        self._timer = None
        try:
            self._dispatch(self._state.poll(self._clock()))
        finally:
            self._after()  # or no deadline is looked at again

    async def _read(self, link: _Link) -> None:
        while not link.stop.is_set():
            data = await link.transport.recv()
            if not data or link is not self._link:
                return
            self._dispatch(self._state.receive(data, self._clock()))
            self._after()
            await asyncio.sleep(0)  # a transport with data ready must not stop the other tasks

    async def _write(self, link: _Link) -> None:
        while True:
            await link.wake.wait()
            link.wake.clear()
            if link is not self._link:
                return
            data = self._state.data_to_send()
            link.sending = len(data)
            for start in range(0, len(data), _PIECE):
                piece = data[start : start + _PIECE]
                await link.transport.send(piece)
                link.sending -= len(piece)
                self._notify()

    async def _end_link(self) -> None:
        if self._link is not None:
            self._link.stop.set()
            await self._link.done.wait()

    def _drop(self, link: _Link) -> None:
        if self._link is link:
            self._link = None
            self._state.disconnected()
            self._after()
            self._notify()

    def _dispatch(self, events: list[Event]) -> None:
        for event in events:
            match event:
                case Ready():
                    self._notify()
                case Closed(status):
                    if self._link is not None:
                        self._link.error = ProtocolError(status)
                        self._link.stop.set()
                case ReplyReceived(request_id, number, body, error):
                    if pending := self._pending.get(request_id):
                        pending.replies.append(Reply(number, body, error))
                case Ended(request_id, outcome):
                    pending = self._pending.pop(request_id, None)
                    if pending is not None and not pending.future.done():
                        pending.future.set_result(Result(outcome, tuple(pending.replies)))
                    self._notify()
                case RequestReceived():
                    self._start(event)

    def _start(self, event: RequestReceived) -> None:
        handler = self._handlers.get(event.key)
        if handler is None:
            self._state.refuse(event.id, no_handler=True)
        elif len(self._running) >= self.config.max_running:
            self._state.refuse(event.id)
        else:
            task = asyncio.create_task(self._handle(handler, event))
            self._running.add(task)
            task.add_done_callback(self._running.discard)

    def _answer_failure(self, request: Request, error: Exception, *, replied: bool) -> None:
        """Refuses a request whose handler raised ``Busy``, and reports any other error."""
        if isinstance(error, Busy) and not replied:
            self._state.refuse(request.id)
            return
        _log.error("the handler of %r failed", request.key, exc_info=error)
        message = f"{type(error).__name__}: {error}".encode(errors="replace")
        self._state.reply(request.id, message[: self.config.max_length], error=True)
        self._state.end(request.id)

    async def _handle(self, handler: Handler, event: RequestReceived) -> None:
        """Runs a handler, and ends its request in the way that matches how it finished."""
        run = _Run()

        def remaining() -> float:
            return max(0, event.deadline - self._clock()) / 1000

        async def send(body: bytes, error: bool) -> None:  # noqa: FBT001
            await self._drain()
            if run.done:
                msg = "the handler of this request has finished"
                raise RuntimeError(msg)
            self._state.reply(event.id, body, error=error)
            run.replies += 1
            self._after()

        request = Request(event, remaining, send, self.name)
        limit = asyncio.timeout(remaining())
        try:
            async with limit:
                result = await handler(request)
                if result is not None:
                    await request.reply(result)
        except Exception as error:  # noqa: BLE001 -- every failure of a handler is answered
            if limit.expired():
                self._state.give_up(request.id)
            else:
                self._answer_failure(request, error, replied=run.replies > 0)
        except BaseException:  # cancelled: it did not finish
            self._state.give_up(request.id)
            raise
        else:
            if limit.expired():  # the handler kept running through its cancellation
                self._state.give_up(request.id)
            else:
                self._state.end(request.id)
        finally:
            run.done = True
            self._after()
