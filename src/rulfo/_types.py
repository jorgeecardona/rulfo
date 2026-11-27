"""The settings, values, events and errors of the Python API."""

from __future__ import annotations

import enum
from dataclasses import dataclass

_MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Config:
    """The settings of one peer. Times are in seconds.

    Args:
        max_length: Largest key plus body accepted.
        handshake: Longest wait for the other side after a connection starts.
        keep: How long a request id is kept after its deadline. Not below ``handshake``.
        max_requests: Open requests this side can have.
        max_ids: Request ids this side keeps as the replier. New requests above
            ``max_ids / (deadline + keep)`` per second are refused as busy.
        max_deadline: A request with a deadline further away is refused as busy.
        replay_bytes: Memory for sent replies, to send them again after a reconnect. 0 stores none.
        replay_bytes_per_request: The same for one request. A request that goes over stores none.
        max_running: Handlers that run at once. Above it a request is refused as busy.
        send_buffer: ``request`` and ``reply`` wait while more bytes than this are not yet sent.
    """

    max_length: int = 16 * _MIB
    handshake: float = 5.0
    keep: float = 60.0
    max_requests: int = 64
    max_ids: int = 4096
    max_deadline: float = 3600.0
    replay_bytes: int = 16 * _MIB
    replay_bytes_per_request: int = _MIB
    max_running: int = 64
    send_buffer: int = _MIB


class Outcome(enum.Enum):
    """How a request ended."""

    COMPLETED = "completed"
    """The handler ran and every reply it sent arrived."""
    BUSY = "busy"
    """Refused. It did not run."""
    EXPIRED = "expired"
    """The deadline passed first. It may have run."""
    UNROUTABLE = "unroutable"
    """No handler for the key, or no connection to send it on. It did not run."""


class RulfoError(Exception):
    """The base of the errors of rulfo."""


@dataclass(frozen=True, slots=True)
class Reply:
    """One reply. ``error`` marks a reply that reports a failure of the handler."""

    number: int
    body: bytes
    error: bool


@dataclass(frozen=True, slots=True)
class Result:
    """The outcome of a request and the replies that arrived."""

    outcome: Outcome
    replies: tuple[Reply, ...]

    def one(self) -> bytes:
        """The body of the only reply of a completed request.

        Raises:
            NotCompleted: The outcome is not ``completed``.
            RemoteError: The reply is an error reply.
            ValueError: The request completed with no reply or with more than one.
        """
        if self.outcome is not Outcome.COMPLETED:
            raise NotCompleted(self)
        if len(self.replies) != 1:
            msg = f"expected one reply, got {len(self.replies)}"
            raise ValueError(msg)
        reply = self.replies[0]
        if reply.error:
            raise RemoteError(reply.body.decode(errors="replace"))
        return reply.body


class NotCompleted(RulfoError):  # noqa: N818
    """The request has another outcome than ``completed``."""

    def __init__(self, result: Result) -> None:
        super().__init__(result.outcome.value)
        self.result = result


class RemoteError(RulfoError):
    """The handler replied with an error."""


class Busy(RulfoError):  # noqa: N818
    """Raised by a handler, before its first reply, to refuse the request. It did not run."""


class PeerClosed(RulfoError):  # noqa: N818
    """The peer was closed."""


class ProtocolError(RulfoError):
    """The other side broke the wire format. ``status`` is the name of the core status."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


class CallError(RulfoError):
    """A call that is not valid now. ``status`` is the name of the core status."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


@dataclass(frozen=True, slots=True)
class Ready:
    """The connection can be used."""


@dataclass(frozen=True, slots=True)
class Closed:
    """The core ends the connection: close the transport and call ``disconnected``."""

    status: str


@dataclass(frozen=True, slots=True)
class ReplyReceived:
    """A reply to a request of this side."""

    id: bytes
    number: int
    body: bytes
    error: bool


@dataclass(frozen=True, slots=True)
class Ended:
    """A request of this side ended with ``outcome`` after ``replies`` replies."""

    id: bytes
    outcome: Outcome
    replies: int


@dataclass(frozen=True, slots=True)
class RequestReceived:
    """A request to run. Each one needs ``end``, ``refuse`` or ``give_up``.

    ``deadline`` is in milliseconds on this side's clock. ``repeat`` means that the requester
    sent it before and this side does not know the id: a process that ran earlier may have run it.
    """

    id: bytes
    key: str
    body: bytes
    deadline: int
    repeat: bool


Event = Ready | Closed | ReplyReceived | Ended | RequestReceived
