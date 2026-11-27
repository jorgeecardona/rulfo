"""Messaging where a request gets zero, one or many replies and always an end."""

from rulfo._peer import Handler, Peer, Request
from rulfo._server import Server
from rulfo._state import PeerState
from rulfo._transport import StreamTransport, Transport
from rulfo._types import (
    Busy,
    CallError,
    Closed,
    Config,
    Ended,
    Event,
    NotCompleted,
    Outcome,
    Overloaded,
    PeerClosed,
    ProtocolError,
    Ready,
    RemoteError,
    Reply,
    ReplyReceived,
    RequestReceived,
    Result,
    RulfoError,
)

__version__ = "0.1.9"  # the single source of truth; pyproject reads it via [tool.hatch.version]

__all__ = [
    "Busy",
    "CallError",
    "Closed",
    "Config",
    "Ended",
    "Event",
    "Handler",
    "NotCompleted",
    "Outcome",
    "Overloaded",
    "Peer",
    "PeerClosed",
    "PeerState",
    "ProtocolError",
    "Ready",
    "RemoteError",
    "Reply",
    "ReplyReceived",
    "Request",
    "RequestReceived",
    "Result",
    "RulfoError",
    "Server",
    "StreamTransport",
    "Transport",
]
