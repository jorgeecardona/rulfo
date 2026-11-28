"""``Server``: many remote parties, one ``Peer`` for each."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from rulfo._peer import Handler, Peer, check_request, timeout_ms
from rulfo._transport import Transport
from rulfo._types import Config, Outcome, PeerClosed, Result


@dataclass(eq=False)
class _Party:
    peer: Peer
    serving: int = 0  # calls of serve that have not returned


class Server:
    """One ``Peer`` for each remote party, found by name.

    The program accepts the connections, checks who is calling, and passes each one to ``serve``
    with that name. The ``Peer`` of a name stays across reconnects, so a request that is sent
    again does not run twice. A ``Peer`` that is idle and has no connection is dropped when the
    next connection arrives.

    Each ``Peer`` allocates memory (about 235 KB with the default ``max_ids``), and the limits of
    ``config`` count for each party separately.

    Use it from one event loop.

    Args:
        handlers: The handlers of every party. ``Request.caller`` is the name of the caller.
        config: The settings of every ``Peer``.
        clock: The clock of every ``Peer``.
    """

    def __init__(
        self,
        *,
        handlers: Mapping[str, Handler] | None = None,
        config: Config | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.config = config or Config()
        self._handlers = dict(handlers or {})
        self._clock = clock
        self._parties: dict[str, _Party] = {}
        self._closed = False

    @property
    def connected(self) -> tuple[str, ...]:
        """The names a request can be sent to now, sorted."""
        return tuple(sorted(n for n, party in self._parties.items() if party.peer.connected))

    async def serve(self, name: str, transport: Transport) -> None:
        """Uses one connection from ``name`` until it ends, and closes it.

        A second connection with the same name replaces the first, whose call returns. Pass a
        name that you checked: whoever gives a name gets its requests.

        Raises:
            PeerClosed: ``serve`` was called after ``aclose``.
            ProtocolError: The other side broke the wire format, or the handshake took too long.
            Overloaded: The other side takes too little of what this side sends.
            Exception: What ``transport`` raises.
        """
        if self._closed:
            await transport.close()
            raise PeerClosed
        self._drop_idle()
        party = self._party(name)
        party.serving += 1
        try:
            await party.peer.serve(transport)
        finally:
            party.serving -= 1

    async def request(
        self,
        name: str,
        key: str,
        body: bytes = b"",
        *,
        timeout: float,  # noqa: ASYNC109 -- the deadline that goes on the wire
    ) -> Result:
        """Sends a request to ``name``. See ``Peer.request``.

        The outcome is ``unroutable`` when ``name`` has no connection. During the handshake of a
        connection the call waits.

        Raises:
            ValueError: An empty key, a timeout out of range, or a key and body above the
                largest length.
            PeerClosed: ``aclose`` was called.
        """
        if self._closed:
            raise PeerClosed
        party = self._parties.get(name)
        if party is None:
            check_request(key, body, self.config.max_length)
            timeout_ms(timeout)
            return Result(Outcome.UNROUTABLE, ())
        return await party.peer.request(key, body, timeout=timeout)

    async def request_copy(  # noqa: PLR0913
        self,
        name: str,
        request_id: bytes,
        key: str,
        body: bytes = b"",
        *,
        deadline_ms: int,
        sent_before: bool,
    ) -> Result:
        """Sends to ``name`` a request whose id and deadline the caller chose.

        See ``Peer.request_copy``. With ``sent_before`` true the call also waits for a name that
        has not connected yet.

        Raises:
            ValueError: As ``Peer.request_copy``.
            CallError: As ``Peer.request_copy``.
            PeerClosed: ``aclose`` was called.
        """
        if self._closed:
            raise PeerClosed
        party = self._party(name)
        try:
            return await party.peer.request_copy(
                request_id, key, body, deadline_ms=deadline_ms, sent_before=sent_before
            )
        finally:
            if party.serving == 0 and party.peer.idle and self._parties.get(name) is party:
                del self._parties[name]

    async def aclose(self) -> None:
        """Closes every ``Peer``. If this call is cancelled, call it again.

        Every connection is closed and every handler is cancelled before the first wait, so a
        handler that takes long to stop delays no other party.
        """
        self._closed = True
        # In this task, not one task for each party: a handler that calls this must not be
        # waited for.
        closing = [
            (name, party.peer, party.peer._close_now())  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
            for name, party in self._parties.items()
        ]
        for name, peer, waits in closing:
            await peer._wait_closed(waits)  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
            self._parties.pop(name, None)

    def _party(self, name: str) -> _Party:
        party = self._parties.get(name)
        if party is None:
            peer = Peer(handlers=self._handlers, config=self.config, clock=self._clock, name=name)
            party = self._parties[name] = _Party(peer)
        return party

    def _drop_idle(self) -> None:
        for name, party in list(self._parties.items()):
            if party.serving == 0 and party.peer.idle:
                del self._parties[name]
