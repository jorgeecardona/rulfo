"""``Peer``: one remote party on asyncio, over ``PeerState``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
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
    PeerClosed,
    ProtocolError,
    Ready,
    Reply,
    ReplyReceived,
    RequestReceived,
    Result,
)

_log = logging.getLogger("rulfo")

_ID_SIZE = 16
_MAX_KEY = 0xFFFF
# The loop's timers stop while the machine is suspended and the clock of rulfo does not, so no
# wait is longer than this.
_LONGEST_WAIT_MS = 1000


def _boot_clock() -> int:
    """Milliseconds that never go back and count the time the machine is suspended."""
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1_000_000


@dataclass(eq=False)
class _Link:
    """One connection, from ``serve``."""

    transport: Transport
    wake: asyncio.Event = field(default_factory=asyncio.Event)  # bytes wait to be sent
    stop: asyncio.Event = field(default_factory=asyncio.Event)  # end this connection
    done: asyncio.Event = field(default_factory=asyncio.Event)  # nothing more is read from it
    sending: int = 0  # bytes given to the transport and not yet sent
    error: ProtocolError | None = None


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

    ``repeat`` means that the requester sent it before and this peer does not know the id: a
    process that ran earlier may have run it. A handler that must not run twice across a restart
    checks its own record when ``repeat`` is true.
    """

    def __init__(
        self,
        event: RequestReceived,
        remaining: Callable[[], float],
        send: Callable[[bytes, bool], Awaitable[None]],
    ) -> None:
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
    A second ``Peer`` for the same party runs a repeated request again. A server keeps them in a
    dict by the authenticated name of the caller, and can drop one when ``idle`` is true and no
    connection serves it.

    Use it from one event loop.

    Args:
        handlers: The handler for each key this side serves. It returns the one reply, or None
            after it sent its replies with ``Request.reply``. It raises ``Busy`` to refuse.
        config: The settings.
        clock: Milliseconds that never go back and count suspended time. Processes that send
            copies of one id need a shared clock (rule 14 in the operations page).
    """

    def __init__(
        self,
        *,
        handlers: Mapping[str, Handler] | None = None,
        config: Config | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
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
        return self._state.idle and not self._running

    async def serve(self, transport: Transport) -> None:
        """Uses one connection until it ends, and closes it.

        A call replaces the connection of an earlier call, which then returns. The caller decides
        when to connect again.

        Raises:
            PeerClosed: ``aclose`` was called.
            ProtocolError: The other side broke the wire format, or the handshake took too long.
            Exception: What ``transport`` raises.
        """
        async with self._replace:
            await self._end_link()
            if self._closed:
                await transport.close()
                raise PeerClosed
            link = _Link(transport)
            self._link = link
            self._state.connected(self._clock())
            self._after()
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
            await asyncio.gather(*tasks, return_exceptions=True)
            self._drop(link)
            link.done.set()
            await transport.close()

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
        size = len(key.encode())
        if not 0 < size <= _MAX_KEY or size + len(body) > self.config.max_length:
            msg = f"a key of {size} bytes with a body of {len(body)} cannot be sent"
            raise ValueError(msg)
        if not timeout > 0:
            msg = "timeout must be above 0"
            raise ValueError(msg)
        deadline = self._clock() + max(1, round(timeout * 1000))
        while True:
            if self._closed:
                raise PeerClosed
            now = self._clock()
            if now >= deadline:
                return Result(Outcome.EXPIRED, ())
            if self._link is None:
                return Result(Outcome.UNROUTABLE, ())
            if (
                self._state.ready
                and self._state.open_requests < self.config.max_requests
                and self._buffered() <= self.config.send_buffer
            ):
                break
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(min(deadline - now, _LONGEST_WAIT_MS) / 1000):
                    await self._changed.wait()
        request_id = os.urandom(_ID_SIZE)
        self._state.request(request_id, key, body, deadline, now)
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

    async def aclose(self) -> None:
        """Closes the connection, fails the open requests and cancels the handlers."""
        self._closed = True
        # The connection goes first: a cancelled handler must not send an end.
        async with self._replace:
            await self._end_link()
        pending, self._pending = self._pending, {}
        for request_id, waiting in pending.items():
            self._state.abandon(request_id)
            if not waiting.future.done():
                waiting.future.set_exception(PeerClosed())
        handlers = [task for task in self._running if task is not asyncio.current_task()]
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        self._arm()
        self._notify()

    def _buffered(self) -> int:
        return self._state.unsent + (self._link.sending if self._link else 0)

    async def _drain(self) -> None:
        """Waits while the connection has too many bytes to send."""
        while self._link is not None and self._buffered() > self.config.send_buffer:
            await self._changed.wait()

    def _notify(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def _after(self) -> None:
        """Runs after every call of the state: bytes may wait and the next time may differ."""
        if self._link is not None and self._state.unsent:
            self._link.wake.set()
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
        self._dispatch(self._state.poll(self._clock()))
        self._after()

    async def _read(self, link: _Link) -> None:
        while not link.stop.is_set():
            data = await link.transport.recv()
            if not data:
                return
            self._dispatch(self._state.receive(data, self._clock()))
            self._after()
            await asyncio.sleep(0)  # a transport with data ready must not stop the other tasks

    async def _write(self, link: _Link) -> None:
        while True:
            await link.wake.wait()
            link.wake.clear()
            data = self._state.data_to_send()
            if data:
                link.sending = len(data)
                await link.transport.send(data)
                link.sending = 0
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

        request = Request(event, remaining, send)
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
