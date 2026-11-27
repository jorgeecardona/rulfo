"""``PeerState``: one remote party, without I/O and without a clock, on the C core."""

from __future__ import annotations

import heapq
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rulfo._core import ffi, lib
from rulfo._types import (
    CallError,
    Closed,
    Config,
    Ended,
    Event,
    Outcome,
    Ready,
    ReplyReceived,
    RequestReceived,
)

_CData = Any  # what cffi returns has no type
_ID_SIZE = 16
_NEVER = 2**64 - 1
_STATUS: dict[int, str] = dict(ffi.typeof("rf_status").elements)
_BAD_VALUES = {lib.RF_ERR_KEY, lib.RF_ERR_KEY_LENGTH, lib.RF_ERR_LENGTH}
_OUTCOMES = {
    lib.RF_COMPLETED: Outcome.COMPLETED,
    lib.RF_BUSY: Outcome.BUSY,
    lib.RF_EXPIRED: Outcome.EXPIRED,
    lib.RF_UNROUTABLE: Outcome.UNROUTABLE,
}


@dataclass(frozen=True, slots=True)
class _Open:
    """A request of this side without an outcome, kept to send it again."""

    key: bytes
    body: bytes
    deadline: int


@dataclass(slots=True)
class _Taken:
    """A request this side took. ``replies`` is None when they cannot be sent again."""

    deadline: int
    replies: list[tuple[int, bytes]] | None
    size: int = 0


def _fail(status: int) -> Exception:
    name = _STATUS[status]
    return ValueError(name) if status in _BAD_VALUES else CallError(name)


def _id(request_id: bytes) -> _CData:
    if len(request_id) != _ID_SIZE:
        msg = f"a request id is {_ID_SIZE} bytes, not {len(request_id)}"
        raise ValueError(msg)
    return ffi.new("rf_id *", {"bytes": request_id})


def _copy(body: bytes) -> bytes:
    """``body`` as bytes that nothing else can change. An int or a str raises ``TypeError``."""
    return bytes(memoryview(body))


def _bytes(data: _CData) -> bytes:
    """A copy of an ``rf_bytes``."""
    return bytes(ffi.buffer(data.ptr, data.len)) if data.len else b""


def _config(config: Config) -> _CData:
    made = ffi.new("rf_config *")
    lib.rf_config_default(made)
    if min(config.replay_bytes, config.replay_bytes_per_request) < 0:
        msg = "a replay limit cannot be negative"
        raise ValueError(msg)
    try:
        made.max_length = config.max_length
        made.handshake_ms = round(config.handshake * 1000)
        made.keep_ms = round(config.keep * 1000)
        made.max_requests = config.max_requests
        made.max_ids = config.max_ids
        made.max_deadline_ms = round(config.max_deadline * 1000)
    except OverflowError as error:
        msg = f"a setting is out of range: {config}"
        raise ValueError(msg) from error
    return made


class PeerState:
    """The state for one remote party: its open requests, the ids it sent and the bytes to send.

    It does no I/O and reads no clock. Every ``now`` and deadline is in milliseconds on one clock
    that never goes back, does not jump and counts the time the machine is suspended.

    For each connection call ``connected``, send what ``data_to_send`` returns after every call,
    pass what arrives to ``receive``, call ``poll`` at ``next_time``, and call ``disconnected``
    when the connection ends or after a ``Closed`` event. Keep the object for the next one: one
    ``PeerState`` stands for one remote party, and a second one for the same party would run a
    repeated request again.

    It sends open requests again after a reconnect, and sends stored replies again when the other
    side asks, without events.
    """

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        settings = _config(self.config)
        size = lib.rf_peer_size(settings)
        if size == 0:
            msg = f"a setting is out of range: {self.config}"
            raise ValueError(msg)
        memory = lib.malloc(size)
        if memory == ffi.NULL:
            raise MemoryError
        self._memory = ffi.gc(memory, lib.free, size)
        made = ffi.new("rf_peer **")
        status = lib.rf_peer_init(self._memory, size, settings, made)
        if status != lib.RF_OK:
            raise _fail(status)
        self._peer = made[0]
        self._header = ffi.new("uint8_t[]", lib.RF_HEADER_SIZE)
        self._event = ffi.new("rf_event *")
        self._in = bytearray()
        self._out = bytearray()
        self._ready = False
        self._open: dict[bytes, _Open] = {}
        self._taken: dict[bytes, _Taken] = {}
        self._stored = 0
        self._free_at: list[tuple[int, bytes]] = []  # a heap of (deadline, id) with stored replies

    @property
    def ready(self) -> bool:
        """Whether the connection can carry a request."""
        return self._ready

    @property
    def open_requests(self) -> int:
        """Requests of this side without an outcome."""
        return len(self._open)

    @property
    def idle(self) -> bool:
        """No open request and no kept id: the object can be dropped without running one twice."""
        return not self._open and not self._taken

    @property
    def unsent(self) -> int:
        """Bytes that ``data_to_send`` would return."""
        return len(self._out)

    @property
    def open_bytes(self) -> int:
        """Bytes of the keys and bodies kept to send open requests again, with their headers."""
        return sum(
            lib.RF_HEADER_SIZE + len(kept.key) + len(kept.body) for kept in self._open.values()
        )

    @property
    def stored_bytes(self) -> int:
        """Memory counted for replies kept to be sent again."""
        return self._stored

    def connected(self, now: int) -> None:
        """Starts a connection. Bytes of an earlier one are dropped."""
        self._reset()
        lib.rf_connected(self._peer, now)
        self._flush()

    def disconnected(self) -> None:
        """Ends the connection. Requests, ids and stored replies are kept."""
        lib.rf_disconnected(self._peer)
        self._reset()

    def receive(self, data: bytes, now: int) -> list[Event]:
        """Reads bytes from the other side, cut anywhere, and returns what happened."""
        events: list[Event] = []
        self._in += data
        buffer = ffi.from_buffer("uint8_t[]", self._in)
        used = ffi.new("size_t *")
        offset = 0
        try:
            while True:
                length = len(self._in) - offset
                status = lib.rf_input(self._peer, buffer + offset, length, now, used, self._event)
                offset += used[0]
                self._handle(now, events)
                self._flush()
                if status not in (lib.RF_OK, lib.RF_WOULD_BLOCK):
                    break
        finally:
            ffi.release(buffer)
            del self._in[:offset]
        if status != lib.RF_NEED_MORE:
            self._in.clear()  # a protocol error, or no connection: nothing more is read
        events += self.poll(now)
        return events

    def poll(self, now: int) -> list[Event]:
        """Returns what is due at ``now``: expired requests and a handshake that took too long."""
        events: list[Event] = []
        while True:
            lib.rf_poll(self._peer, now, self._event)
            if self._event.kind == lib.RF_EV_NONE:
                break
            self._handle(now, events)
            self._flush()
        # Past the deadline the requester drops every reply: the stored ones have no use.
        while self._free_at and self._free_at[0][0] <= now:
            _, request_id = heapq.heappop(self._free_at)
            taken = self._taken.get(request_id)
            if taken is not None and taken.deadline <= now:
                self._unstore(taken)
        return events

    def next_time(self) -> int | None:
        """When to call ``poll`` next. A time that has passed means now. None: nothing is due."""
        core = lib.rf_next_time(self._peer)
        while self._free_at:
            deadline, request_id = self._free_at[0]
            taken = self._taken.get(request_id)
            if taken is not None and taken.replies is not None and taken.deadline == deadline:
                return min(core, deadline)
            heapq.heappop(self._free_at)
        return None if core == _NEVER else core

    def data_to_send(self) -> bytes:
        """All bytes that wait, in the order they go on the wire."""
        data = bytes(self._out)
        self._out.clear()
        return data

    def request(  # noqa: PLR0913
        self,
        request_id: bytes,
        key: str,
        body: bytes,
        deadline: int,
        now: int,
        *,
        sent_before: bool = False,
    ) -> None:
        """Sends a request. Its outcome comes later as ``Ended``, once.

        Args:
            request_id: 16 bytes that no other request uses.
            key: Which handler runs it. Not empty.
            body: The bytes for the handler.
            deadline: When the request expires.
            now: The time of this call.
            sent_before: An earlier copy of this id was sent, for example by another process,
                and no end arrived.

        Raises:
            ValueError: A key or a length that the wire format refuses.
            CallError: The connection cannot be used, the deadline has passed, too many requests
                are open, or the id is open.
        """
        if request_id in self._open:
            raise CallError(_STATUS[lib.RF_ERR_STATE])
        kept = _Open(key.encode(), _copy(body), deadline)
        status = self._send_request(request_id, kept, now, repeat=sent_before)
        if status != lib.RF_OK:
            raise _fail(status)
        self._open[request_id] = kept

    def abandon(self, request_id: bytes) -> None:
        """Stops waiting for a request: no event is given for it. It may still run.

        Does nothing when the request is not open. Do not use the id again.
        """
        lib.rf_abandon(self._peer, _id(request_id)[0])
        self._open.pop(request_id, None)

    def reply(self, request_id: bytes, body: bytes, *, error: bool = False) -> None:
        """Sends one reply to a received request, or only stores it when there is no connection.

        Raises:
            ValueError: A body longer than the wire format or ``max_length`` allows.
            CallError: The id is not known, or the request has ended.
        """
        ident = _id(request_id)
        kept = _copy(body)
        flags = lib.RF_REPLY_ERROR if error else 0
        status = self._write(
            lambda header: lib.rf_reply(self._peer, ident[0], len(kept), flags, header), kept
        )
        if status not in (lib.RF_OK, lib.RF_OFFLINE):
            raise _fail(status)
        self._store(request_id, flags, kept)

    def end(self, request_id: bytes) -> None:
        """Ends a received request: the requester gets ``completed``.

        Raises:
            CallError: The id is not known, or the request has ended.
        """
        ident = _id(request_id)
        status = self._write(lambda header: lib.rf_end(self._peer, ident[0], header))
        if status not in (lib.RF_OK, lib.RF_OFFLINE):
            raise _fail(status)

    def give_up(self, request_id: bytes) -> None:
        """Closes a received request that did not finish. Nothing is sent, now or on a repeat.

        The requester gets ``expired``: the request may have run in part.

        Raises:
            CallError: The id is not known, or the request has ended.
        """
        self._flush()
        status = lib.rf_end(self._peer, _id(request_id)[0], self._header)
        if status not in (lib.RF_OK, lib.RF_OFFLINE):
            raise _fail(status)
        taken = self._taken.get(request_id)
        if taken is not None:
            self._unstore(taken)

    def refuse(self, request_id: bytes, *, no_handler: bool = False) -> None:
        """Refuses a received request before its first reply, instead of running it.

        The refusal is not sent for a request that was sent again, or whose connection ended.

        Raises:
            CallError: The id is not known, or a reply or the end was sent.
        """
        ident = _id(request_id)
        flag = lib.RF_END_NO_HANDLER if no_handler else lib.RF_END_BUSY
        status = self._write(lambda header: lib.rf_refuse(self._peer, ident[0], flag, header))
        if status not in (lib.RF_OK, lib.RF_DROPPED):
            raise _fail(status)
        self._drop_taken(request_id)

    def _reset(self) -> None:
        self._ready = False
        self._in.clear()
        self._out.clear()

    def _flush(self) -> None:
        """Moves the frames the core made behind the bytes that already wait."""
        chunk = ffi.new("uint8_t[]", 256)
        while count := lib.rf_output(self._peer, chunk, len(chunk)):
            self._out += ffi.buffer(chunk, count)

    def _write(self, call: Callable[[_CData], int], *parts: bytes) -> int:
        """Makes a core call that writes a header, and queues the header and ``parts`` on OK."""
        self._flush()  # the core wants its own frames before this header
        status = call(self._header)
        if status == lib.RF_OK:
            self._out += ffi.buffer(self._header)
            for part in parts:
                self._out += part
        return status

    def _send_request(self, request_id: bytes, kept: _Open, now: int, *, repeat: bool) -> int:
        ident = _id(request_id)
        key = ffi.from_buffer("uint8_t[]", kept.key)
        wire_key = ffi.new("rf_bytes *", {"ptr": key, "len": len(kept.key)})
        return self._write(
            lambda header: lib.rf_request(
                self._peer,
                ident[0],
                wire_key[0],
                len(kept.body),
                kept.deadline,
                repeat,
                now,
                header,
            ),
            kept.key,
            kept.body,
        )

    def _handle(self, now: int, events: list[Event]) -> None:
        """Acts on the event of the core: gives it to the caller, or answers it here."""
        event = self._event
        kind = event.kind
        if kind == lib.RF_EV_NONE:
            return
        request_id = bytes(event.id.bytes)
        if kind == lib.RF_EV_READY:
            self._ready = True
            events.append(Ready())
        elif kind == lib.RF_EV_CLOSE:
            self._ready = False
            self._out.clear()
            events.append(Closed(_STATUS[event.status]))
        elif kind == lib.RF_EV_REPLY:
            error = bool(event.flags & lib.RF_REPLY_ERROR)
            events.append(ReplyReceived(request_id, event.number, _bytes(event.body), error))
        elif kind == lib.RF_EV_OUTCOME:
            self._open.pop(request_id, None)
            events.append(Ended(request_id, _OUTCOMES[event.outcome], event.number))
        elif kind == lib.RF_EV_RESEND:
            status = self._send_request(request_id, self._open[request_id], now, repeat=True)
            if status not in (lib.RF_OK, lib.RF_ERR_EXPIRED):
                raise _fail(status)
        elif kind == lib.RF_EV_REQUEST:
            self._take(request_id, event.deadline)
            events.append(
                RequestReceived(
                    request_id,
                    _bytes(event.key).decode(),
                    _bytes(event.body),
                    event.deadline,
                    bool(event.flags & lib.RF_REQUEST_REPEAT),
                )
            )
        elif kind == lib.RF_EV_REPEAT:
            self._send_again(request_id)
        else:
            self._drop_taken(request_id)  # RF_EV_FORGOT

    def _take(self, request_id: bytes, deadline: int) -> None:
        storing = self.config.replay_bytes > 0
        self._taken[request_id] = _Taken(deadline, [] if storing else None)

    def _store(self, request_id: bytes, flags: int, body: bytes) -> None:
        taken = self._taken[request_id]
        if taken.replies is None:
            return
        cost = lib.RF_HEADER_SIZE + len(body)
        if (
            taken.size + cost > self.config.replay_bytes_per_request
            or self._stored + cost > self.config.replay_bytes
        ):
            # A replay starts at reply 0, so a part of the replies is of no use.
            self._unstore(taken)
            return
        if not taken.replies:  # the first stored reply: there is something to free later
            heapq.heappush(self._free_at, (taken.deadline, request_id))
        taken.replies.append((flags, body))
        taken.size += cost
        self._stored += cost

    def _unstore(self, taken: _Taken) -> None:
        self._stored -= taken.size
        taken.size = 0
        taken.replies = None

    def _drop_taken(self, request_id: bytes) -> None:
        taken = self._taken.pop(request_id, None)
        if taken is not None:
            self._unstore(taken)

    def _send_again(self, request_id: bytes) -> None:
        """Sends every stored reply and the end, or nothing when they are not stored."""
        taken = self._taken.get(request_id)
        if taken is None or taken.replies is None:
            return
        ident = _id(request_id)
        for number, (flags, body) in enumerate(taken.replies):
            status = self._write(
                lambda header, number=number, flags=flags, body=body: lib.rf_reply_again(
                    self._peer, ident[0], number, len(body), flags, header
                ),
                body,
            )
            if status != lib.RF_OK:
                raise _fail(status)
        status = self._write(lambda header: lib.rf_end_again(self._peer, ident[0], header))
        if status not in (lib.RF_OK, lib.RF_ERR_STATE):  # ERR_STATE: not ended yet
            raise _fail(status)
