"""``PeerState`` replays runs of the request model on the C core.

Each walk takes random actions of the model and makes the matching calls on two ``PeerState``
objects for each replier. After every action they agree on the outcome, the replies received from
each replier, the requests given to each handler and the replies delivered after the outcome. Half
of the walks store replies and send them again, and half store none.

Not replayed, because one clock and one ``PeerState`` per replier cannot produce them:

- a copy read from a closed connection;
- a deadline that passes on one side only: before ``deadline`` no connection expires and no id
  is forgotten, and after it no copy is read.
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field

import pytest

from links import frames
from request_reply import Replier, Rules, World, request_model
from rulfo import Config, Ended, Event, PeerState, Ready, ReplyReceived, RequestReceived

DEADLINE = 1000
KEEP = 60000
REQUEST_ID = bytes(range(16))
KEY = "op"
BODY = b"ask"


@dataclass
class Pair:
    """The two ``PeerState`` between the requester and one replier, and what is in flight."""

    requester: PeerState
    replier: PeerState
    copies: deque[bytes] = field(default_factory=deque[bytes])
    frames: deque[bytes] = field(default_factory=deque[bytes])
    received: int = 0
    requests: int = 0  # RequestReceived events of the replier
    refused: int = 0  # calls of refuse
    outcome: str | None = None


class Run:
    """The ``PeerState`` side of one walk. ``seen`` collects the situations it went through."""

    def __init__(self, repliers: int, seen: set[str], *, replay: bool) -> None:
        self.now = 0
        self.outcome: str | None = None
        self.late = 0
        self.seen = seen
        config = Config(keep=KEEP / 1000, max_ids=4, replay_bytes=1024 if replay else 0)
        self.pairs = [Pair(PeerState(config), PeerState(config)) for _ in range(repliers)]
        for pair in self.pairs:
            assert self._join(pair) == []

    def _join(self, pair: Pair) -> list[bytes]:
        """Connects the pair and returns the copies the requester sends again by itself."""
        peers = (pair.requester, pair.replier)
        for peer in peers:
            peer.connected(self.now)
        first = [peer.data_to_send() for peer in peers]
        for peer, preface in zip(peers, reversed(first), strict=True):
            assert peer.receive(preface, self.now) == []
        second = [peer.data_to_send() for peer in peers]
        assert pair.replier.receive(second[0], self.now) == [Ready()]
        events = pair.requester.receive(second[1], self.now)
        assert events[0] == Ready()
        self._take(pair, events[1:])
        return frames(pair.requester.data_to_send())

    def _take(self, pair: Pair, events: list[Event]) -> None:
        """Count what the requester of ``pair`` was told."""
        for event in events:
            if isinstance(event, ReplyReceived):
                assert (event.number, event.body) == (pair.received, bytes([pair.received]))
                pair.received += 1
                self.late += self.outcome is not None
            else:
                assert isinstance(event, Ended)
                assert pair.outcome is None
                pair.outcome = event.outcome.value
                assert event.replies == pair.received

    def _combine(self) -> None:
        """One outcome for all repliers. ``PeerState`` gives one for each."""
        if self.outcome is not None:
            return
        outcomes = [pair.outcome for pair in self.pairs]
        if "expired" in outcomes:
            self.outcome = "expired"
        elif all(outcomes):
            self.outcome = "busy" if "busy" in outcomes else "completed"

    def _send(self, pair: Pair) -> None:
        pair.requester.request(REQUEST_ID, KEY, BODY, DEADLINE, self.now)
        pair.copies.append(pair.requester.data_to_send())

    def apply(self, name: str, before: World, after: World) -> None:
        """Do on ``PeerState`` what the model action ``name`` did from ``before`` to ``after``."""
        if name == "send":
            for pair in self.pairs:
                self._send(pair)
        elif name == "retry":
            self.outcome = None
            for pair in self.pairs:
                pair.received, pair.outcome = 0, None
                self._send(pair)
        elif name == "deadline":
            self.now = max(self.now, DEADLINE)
            for pair in self.pairs:
                self._take(pair, pair.requester.poll(self.now))
                assert pair.outcome is not None
        else:
            replier, action = name.split("_", 1)
            self._apply_replier(int(replier[1:]), action, before, after)
        self._combine()

    def _apply_replier(self, i: int, action: str, before: World, after: World) -> None:
        pair = self.pairs[i]
        old, new = before.repliers[i], after.repliers[i]
        if action in ("reply", "end", "refuse"):
            self._answer(pair, action, old, new)
        elif action == "receive":
            self._receive(pair, old, new)
        elif action == "replay":
            self._replay(pair, old.sent, ended=old.phase == "ended")
        elif action == "deliver":
            self._take(pair, pair.requester.receive(pair.frames.popleft(), self.now))
        elif action == "disconnect":
            self._reconnect(pair, resend=new.inbox == (True,))
        elif action == "forget":
            self._forget(pair)
        elif action.startswith("expire"):
            if int(action.removeprefix("expire")) == new.conn:
                self._drop_copies(pair)
        else:
            assert action in ("start", "hang", "crash")

    def _answer(self, pair: Pair, action: str, old: Replier, new: Replier) -> None:
        """A call of the handler: a reply, the end or a refusal."""
        if action == "reply":
            pair.replier.reply(REQUEST_ID, bytes([old.sent]))
        elif action == "end":
            pair.replier.end(REQUEST_ID)
        else:
            pair.refused += 1
            pair.replier.refuse(REQUEST_ID)
        sent = frames(pair.replier.data_to_send())
        if action == "refuse" and len(new.outbox) == len(old.outbox):
            assert sent == []
            self.seen.add("a refusal that is not sent")
            return
        assert len(sent) == 1
        pair.frames.append(sent[0])

    def _receive(self, pair: Pair, old: Replier, new: Replier) -> None:
        events = pair.replier.receive(pair.copies.popleft(), self.now)
        marked = old.inbox[0]
        taken = old.phase not in ("taken", "running") and new.phase in ("taken", "running")
        if taken:
            assert events == [RequestReceived(REQUEST_ID, KEY, BODY, DEADLINE, repeat=marked)]
            pair.requests += 1
        else:
            assert events == []
        assert pair.replier.data_to_send() == b""

    def _replay(self, pair: Pair, sent: int, *, ended: bool) -> None:
        assert pair.replier.receive(pair.copies.popleft(), self.now) == []
        again = frames(pair.replier.data_to_send())
        assert len(again) == sent + ended
        pair.frames.extend(again)
        self.seen.add("replies sent again")

    def _reconnect(self, pair: Pair, *, resend: bool) -> None:
        pair.requester.disconnected()
        pair.replier.disconnected()
        pair.copies.clear()
        pair.frames.clear()
        copies = self._join(pair)
        assert len(copies) == resend
        pair.copies.extend(copies)

    def _drop_copies(self, pair: Pair) -> None:
        """Past the deadline each copy is read and nothing happens."""
        while pair.copies:
            assert pair.replier.receive(pair.copies.popleft(), self.now) == []
            assert pair.replier.data_to_send() == b""

    def _forget(self, pair: Pair) -> None:
        self.now = max(self.now, DEADLINE + KEEP)
        assert pair.replier.poll(self.now) == []
        assert pair.replier.idle
        self._drop_copies(pair)
        self.seen.add("a forgotten id")

    def agrees_with(self, s: World) -> None:
        assert self.outcome == s.outcome
        assert [p.received for p in self.pairs] == [st.received for st in s.streams]
        # Each request the handler got was started, refused, or still waits for that decision.
        assert [p.requests - p.refused for p in self.pairs] == [
            r.runs + (r.taken is not None) for r in s.repliers
        ]
        assert [p.outcome in ("completed", "busy") for p in self.pairs] == [
            st.ended for st in s.streams
        ]
        assert self.late == s.late
        # Before the first id can be forgotten, both keep the same ids.
        if self.now < DEADLINE + KEEP:
            assert [p.replier.idle for p in self.pairs] == [not r.remembers for r in s.repliers]


def _replayable(s: World, name: str, *, replay: bool) -> bool:
    """Whether ``PeerState`` can be driven through the action ``name`` from ``s``."""
    if name.endswith("_old"):
        return False
    if "_expire" in name:
        return s.past_deadline
    if name.endswith("_forget"):
        return s.past_deadline and s.repliers[int(name[1 : name.index("_")])].phase == "ended"
    if name.endswith("_replay") and not replay:
        return False
    if name.endswith("_receive") and replay:
        # PeerState with stored replies sends them again: it never ignores such a copy.
        r = s.repliers[int(name[1 : name.index("_")])]
        if r.inbox[0] and r.remembers and (r.sent > 0 or r.phase == "ended"):
            return False
    return not (s.past_deadline and name.endswith(("_receive", "_replay")))


def _walk(rules: Rules, seed: int, seen: set[str]) -> None:
    choose = random.Random(seed)  # noqa: S311
    model = request_model(rules)
    assert isinstance(model.init, World)
    state: World = model.init
    replay = seed % 2 == 0
    run = Run(rules.repliers, seen, replay=replay)
    trace: list[str] = []
    while True:
        enabled = [
            a
            for a in model.actions
            if a.enabled(state) and _replayable(state, a.name, replay=replay)
        ]
        if not enabled:
            return
        # Faults and the deadline are rarer, so that walks reach the ends of a request.
        rare = ("deadline", "_hang", "_crash")
        weights = [0.15 if a.name.endswith(rare) else 1.0 for a in enabled]
        action = choose.choices(enabled, weights)[0]
        after = action.apply(state)
        trace.append(action.name)
        try:
            run.apply(action.name, state, after)
            run.agrees_with(after)
        except AssertionError as error:
            msg = f"seed {seed}, actions {trace}"
            raise AssertionError(msg) from error
        state = after
        if state.outcome is not None:
            seen.add(state.outcome)
            if state.retries:
                seen.add(f"{state.outcome} after a retry")
            if state.outcome == "completed" and any(r.lost_end for r in state.repliers):
                seen.add("completed after a lost end")


ALWAYS = {
    "completed",
    "busy",
    "expired",
    "completed after a retry",
    "completed after a lost end",
    "a refusal that is not sent",
    "replies sent again",
    "a forgotten id",
}


@pytest.mark.parametrize(
    ("rules", "walks"),
    [
        (Rules(), 4000),
        (Rules(refusals=2, disconnects=2, redeliveries=2), 4000),
        (Rules(repliers=2, max_replies=1, refusals=2), 4000),
    ],
    ids=["one-replier", "more-faults", "two-repliers"],
)
def test_peer_state_replays_the_model(rules: Rules, walks: int) -> None:
    seen: set[str] = set()
    for seed in range(walks):
        _walk(rules, seed, seen)
    assert seen >= ALWAYS, ALWAYS - seen
