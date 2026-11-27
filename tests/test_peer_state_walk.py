"""Random runs of two ``PeerState`` with several requests at once, which the model does not have.

A walk mixes new requests, pieces of the byte streams, replies, ends, refusals, handlers that
give up, reconnects and time. After it, every deadline passes. In every run:

- each request gets exactly one outcome;
- its replies arrive in order, once, with the body the handler sent for that id;
- its handler runs at most once, not counting a run that was refused before it replied;
- ``completed`` means that the handler ended and every reply arrived;
- ``busy`` and ``unroutable`` mean that no handler kept the request.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import pytest

from rulfo import Config, Ended, Event, Outcome, PeerState, ReplyReceived, RequestReceived

STEPS = 120
REFUSED = ("refused", "no handler")


@dataclass
class Handler:
    """What the handler of one request did."""

    sent: int = 0
    state: str = "running"  # then "ended", "refused", "no handler" or "gave up"


@dataclass
class Walk:
    choose: random.Random
    requester: PeerState
    replier: PeerState
    now: int = 0
    to_replier: bytearray = field(default_factory=bytearray)
    to_requester: bytearray = field(default_factory=bytearray)
    replies: dict[bytes, int] = field(default_factory=dict[bytes, int])  # received
    outcomes: dict[bytes, Outcome] = field(default_factory=dict[bytes, Outcome])
    handlers: dict[bytes, Handler] = field(default_factory=dict[bytes, Handler])

    def connect(self) -> None:
        self.to_replier.clear()
        self.to_requester.clear()
        for peer in (self.requester, self.replier):
            peer.disconnected()
            peer.connected(self.now)

    def collect(self) -> None:
        self.to_replier += self.requester.data_to_send()
        self.to_requester += self.replier.data_to_send()

    def move(self, *, all_of_it: bool = False) -> None:
        """Delivers a piece of one of the two byte streams."""
        pipe, peer = self.choose.choice(
            [(self.to_replier, self.replier), (self.to_requester, self.requester)]
        )
        if all_of_it:
            size = len(pipe)
        else:
            size = self.choose.randint(0, min(len(pipe), self.choose.choice((5, 40, 200))))
        piece = bytes(pipe[:size])
        del pipe[:size]
        self.hear(peer.receive(piece, self.now))

    def hear(self, events: list[Event]) -> None:
        for event in events:
            if isinstance(event, RequestReceived):
                before = self.handlers.get(event.id)
                # A refused request did not run, so a copy sent again can still run.
                assert before is None or before.state in REFUSED, "a request ran twice"
                self.handlers[event.id] = Handler()
            elif isinstance(event, ReplyReceived):
                assert event.id not in self.outcomes
                number = self.replies[event.id]
                assert (event.number, event.body) == (number, event.id + bytes([number]))
                self.replies[event.id] += 1
            elif isinstance(event, Ended):
                assert event.id not in self.outcomes, "a second outcome"
                assert event.replies == self.replies[event.id]
                self.outcomes[event.id] = event.outcome
                self.check(event.id, event.outcome)

    def check(self, request_id: bytes, outcome: Outcome) -> None:
        handler = self.handlers.get(request_id)
        if outcome is Outcome.COMPLETED:
            assert handler is not None
            assert (handler.state, handler.sent) == ("ended", self.replies[request_id])
        elif outcome is not Outcome.EXPIRED:
            assert handler is None or handler.state in REFUSED

    def ask(self) -> None:
        if self.requester.ready and self.requester.open_requests < 3:
            request_id = len(self.replies).to_bytes(16, "little")
            deadline = self.now + self.choose.randint(20, 600)
            self.requester.request(request_id, "op", b"ask", deadline, self.now)
            self.replies[request_id] = 0

    def handle(self) -> None:
        running = [i for i, h in self.handlers.items() if h.state == "running"]
        if not running:
            return
        request_id = self.choose.choice(running)
        handler = self.handlers[request_id]
        action = self.choose.choices(("reply", "end", "refuse", "give up"), (6, 3, 1, 1))[0]
        if action == "reply" and handler.sent < 5:
            self.replier.reply(request_id, request_id + bytes([handler.sent]))
            handler.sent += 1
        elif action == "end":
            self.replier.end(request_id)
            handler.state = "ended"
        elif action == "refuse" and handler.sent == 0:
            no_handler = self.choose.random() < 0.5
            self.replier.refuse(request_id, no_handler=no_handler)
            handler.state = "no handler" if no_handler else "refused"
        elif action == "give up":
            self.replier.give_up(request_id)
            handler.state = "gave up"

    def step(self) -> None:
        action = self.choose.choices(
            ("ask", "move", "handle", "reconnect", "time"), (2, 12, 5, 1, 2)
        )[0]
        if action == "ask":
            self.ask()
        elif action == "move":
            self.move()
        elif action == "handle":
            self.handle()
        elif action == "reconnect":
            self.connect()
        else:
            self.now += self.choose.randint(1, 30)
            self.hear(self.requester.poll(self.now))
            self.hear(self.replier.poll(self.now))
        self.collect()


CONFIGS = [
    Config(max_ids=64),
    Config(max_ids=64, replay_bytes=0),
    Config(max_ids=64, replay_bytes_per_request=100),
    Config(max_ids=3),
]


@pytest.mark.parametrize("config", CONFIGS, ids=["replay", "no-replay", "small-replay", "full"])
def test_random_runs_with_several_requests(config: Config) -> None:
    seen: set[Outcome] = set()
    replies = 0
    for seed in range(400):
        walk = Walk(random.Random(seed), PeerState(config), PeerState(config))  # noqa: S311
        try:
            walk.connect()
            walk.collect()
            for _ in range(STEPS):
                walk.step()
            walk.now += 1000
            walk.hear(walk.requester.poll(walk.now))
            assert set(walk.outcomes) == set(walk.replies), "a request without an outcome"
        except AssertionError as error:
            msg = f"seed {seed}"
            raise AssertionError(msg) from error
        seen |= set(walk.outcomes.values())
        replies += sum(walk.replies.values())
    assert seen == set(Outcome)
    assert replies > 100
