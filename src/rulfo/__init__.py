"""Messaging where a request gets zero, one or many replies and always an end."""

from rulfo._state import PeerState
from rulfo._types import (
    Busy,
    CallError,
    Closed,
    Config,
    Ended,
    Event,
    NotCompleted,
    Outcome,
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

__version__ = "0.1.3"  # the single source of truth; pyproject reads it via [tool.hatch.version]

__all__ = [
    "Busy",
    "CallError",
    "Closed",
    "Config",
    "Ended",
    "Event",
    "NotCompleted",
    "Outcome",
    "PeerClosed",
    "PeerState",
    "ProtocolError",
    "Ready",
    "RemoteError",
    "Reply",
    "ReplyReceived",
    "RequestReceived",
    "Result",
    "RulfoError",
]
