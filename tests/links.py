"""Two ``PeerState`` objects joined in memory, for the tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from rulfo import Config, Event, PeerState

HEADER = 32
PREFACE = 8
ID = bytes(range(16))


def frames(data: bytes) -> list[bytes]:
    """``data`` cut into the preface and whole frames."""
    cut: list[bytes] = []
    while data:
        length = int.from_bytes(data[:4], "little")
        size = PREFACE if data.startswith(b"RULF") else HEADER + length
        assert len(data) >= size
        cut.append(data[:size])
        data = data[size:]
    return cut


@dataclass
class Link:
    """A requester and a replier. ``got`` and ``ran`` are the events each one gave."""

    requester: PeerState = field(default_factory=PeerState)
    replier: PeerState = field(default_factory=PeerState)
    now: int = 0
    got: list[Event] = field(default_factory=list[Event])
    ran: list[Event] = field(default_factory=list[Event])

    @classmethod
    def with_config(cls, config: Config) -> Link:
        return cls(PeerState(config), PeerState(config))

    def connect(self) -> None:
        self.requester.connected(self.now)
        self.replier.connected(self.now)
        self.pump()

    def disconnect(self) -> None:
        self.requester.disconnected()
        self.replier.disconnected()

    def pump(self, chunk: int = 1 << 20) -> None:
        """Moves bytes both ways, ``chunk`` at a time, until neither side has any."""
        while self.requester.unsent or self.replier.unsent:
            to_replier = self.requester.data_to_send()
            for at in range(0, len(to_replier), chunk):
                self.ran += self.replier.receive(to_replier[at : at + chunk], self.now)
            to_requester = self.replier.data_to_send()
            for at in range(0, len(to_requester), chunk):
                self.got += self.requester.receive(to_requester[at : at + chunk], self.now)

    def request(self, request_id: bytes = ID, body: bytes = b"ask", deadline: int = 1000) -> None:
        self.requester.request(request_id, "op", body, deadline, self.now)
        self.pump()
