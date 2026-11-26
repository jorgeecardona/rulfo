"""The C core replays runs of the request model, and shows the requester the same things.

Each walk takes random actions of the model and makes the matching calls on the C core. After
every action the two agree on the outcome, the replies received from each replier, the handler
starts and the replies delivered after the outcome.

Not replayed, because one clock and one ``rf_peer`` per replier cannot produce them:

- a copy read from a closed connection;
- a deadline that passes on one side only: before ``deadline`` no connection expires and no id
  is forgotten, and after it no copy is read.
"""

from __future__ import annotations

import ctypes
import random
from collections import deque
from dataclasses import dataclass, field

import pytest

from c_core import EVENT, OUTCOME, STATUS, Event, Peer, build
from request_reply import Replier, Rules, World, request_model

DEADLINE = 1000
KEEP = 60000
REQUEST_ID = bytes(range(16))
KEY = b"op"
_NAMES = {number: name.removeprefix("RF_").lower() for name, number in OUTCOME.items()}


@pytest.fixture(scope="module")
def library(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    return build(tmp_path_factory.mktemp("core"))


@dataclass
class Pair:
    """The two ``rf_peer`` objects between the requester and one replier, and what is in flight."""

    requester: Peer
    replier: Peer
    copies: deque[bytes] = field(default_factory=deque[bytes])
    frames: deque[bytes] = field(default_factory=deque[bytes])
    received: int = 0
    runs: int = 0
    outcome: str | None = None


class Run:
    """The C side of one walk. ``seen`` collects the situations the walk went through."""

    def __init__(self, library: ctypes.CDLL, repliers: int, seen: set[str]) -> None:
        self.now = 0
        self.outcome: str | None = None
        self.late = 0
        self.seen = seen
        self.pairs = [Pair(Peer(library, KEEP), Peer(library, KEEP)) for _ in range(repliers)]
        for pair in self.pairs:
            self._join(pair)

    def _join(self, pair: Pair) -> None:
        peers = (pair.requester, pair.replier)
        for peer in peers:
            peer.connected(self.now)
        first = [peer.output() for peer in peers]
        for peer, preface in zip(peers, reversed(first), strict=True):
            status, used, _ = peer.input(preface, self.now)
            assert (status, used) == (STATUS["RF_OK"], len(preface))
        second = [peer.output() for peer in peers]
        for peer, clock in zip(peers, reversed(second), strict=True):
            _, used, event = peer.input(clock, self.now)
            assert (used, event.kind) == (len(clock), EVENT["RF_EV_READY"])

    def _read(self, peer: Peer, frame: bytes) -> Event:
        status, used, event = peer.input(frame, self.now)
        assert (status, used) == (STATUS["RF_OK"], len(frame))
        return event

    def _take(self, pair: Pair, event: Event) -> None:
        """Count what the requester of ``pair`` was told."""
        if event.kind == EVENT["RF_EV_REPLY"]:
            assert event.body == bytes([pair.received])
            pair.received += 1
            self.late += self.outcome is not None
        elif event.kind == EVENT["RF_EV_OUTCOME"]:
            assert pair.outcome is None
            pair.outcome = _NAMES[event.outcome]
            assert event.number == pair.received

    def _combine(self) -> None:
        """One outcome for all repliers. The core gives one per peer."""
        if self.outcome is not None:
            return
        outcomes = [pair.outcome for pair in self.pairs]
        if "expired" in outcomes:
            self.outcome = "expired"
        elif all(outcomes):
            self.outcome = "busy" if "busy" in outcomes else "completed"

    def _poll_requester(self, pair: Pair) -> list[Event]:
        events: list[Event] = []
        while (event := pair.requester.poll(self.now)).kind != EVENT["RF_EV_NONE"]:
            self._take(pair, event)
            events.append(event)
        return events

    def _send(self, pair: Pair, *, repeat: bool) -> None:
        status, frame = pair.requester.request(REQUEST_ID, KEY, DEADLINE, self.now, repeat=repeat)
        assert status == STATUS["RF_OK"]
        pair.copies.append(frame)

    def apply(self, name: str, before: World, after: World) -> None:
        """Do on the C core what the model action ``name`` did from ``before`` to ``after``."""
        if name == "send":
            for pair in self.pairs:
                self._send(pair, repeat=False)
        elif name == "retry":
            self.outcome = None
            for pair in self.pairs:
                pair.received, pair.outcome = 0, None
                self._send(pair, repeat=False)
        elif name == "deadline":
            self.now = max(self.now, DEADLINE)
            for pair in self.pairs:
                self._poll_requester(pair)
            self.outcome = self.outcome or "expired"
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
        elif action == "start":
            pair.runs += 1
        elif action == "deliver":
            self._take(pair, self._read(pair.requester, pair.frames.popleft()))
        elif action == "disconnect":
            self._reconnect(pair, resend=new.inbox == (True,))
        elif action == "forget":
            self._forget(pair)
        elif action.startswith("expire"):
            if int(action.removeprefix("expire")) == new.conn:
                self._drop_copies(pair)
        else:
            assert action in ("hang", "crash")

    def _answer(self, pair: Pair, action: str, old: Replier, new: Replier) -> None:
        """A call of the handler: a reply, the end or a refusal."""
        if action == "reply":
            status, frame = pair.replier.reply(REQUEST_ID, bytes([old.sent]))
        elif action == "end":
            status, frame = pair.replier.end(REQUEST_ID)
        else:
            status, frame = pair.replier.refuse(REQUEST_ID)
            if len(new.outbox) == len(old.outbox):
                assert status == STATUS["RF_DROPPED"]
                self.seen.add("a refusal that is not sent")
                return
        assert status == STATUS["RF_OK"]
        pair.frames.append(frame)

    def _receive(self, pair: Pair, old: Replier, new: Replier) -> None:
        frame = pair.copies.popleft()
        marked = old.inbox[0]
        event = self._read(pair.replier, frame)
        taken = old.phase not in ("taken", "running") and new.phase in ("taken", "running")
        if taken:
            assert event.kind == EVENT["RF_EV_REQUEST"]
            pair.runs += new.runs - old.runs
        elif marked and old.remembers:
            assert event.kind == EVENT["RF_EV_REPEAT"]
            assert (event.number, event.ended) == (old.sent, old.phase == "ended")
        else:
            assert event.kind == EVENT["RF_EV_NONE"]

    def _replay(self, pair: Pair, sent: int, *, ended: bool) -> None:
        event = self._read(pair.replier, pair.copies.popleft())
        assert event.kind == EVENT["RF_EV_REPEAT"]
        assert (event.number, event.ended) == (sent, ended)
        stored = [pair.replier.reply_again(REQUEST_ID, n, bytes([n])) for n in range(sent)]
        if ended:
            stored.append(pair.replier.end_again(REQUEST_ID))
        for status, frame in stored:
            assert status == STATUS["RF_OK"]
            pair.frames.append(frame)
        self.seen.add("replies sent again")

    def _reconnect(self, pair: Pair, *, resend: bool) -> None:
        pair.requester.disconnected()
        pair.replier.disconnected()
        pair.copies.clear()
        pair.frames.clear()
        self._join(pair)
        asked = [e for e in self._poll_requester(pair) if e.kind == EVENT["RF_EV_RESEND"]]
        assert len(asked) == resend
        if resend:
            self._send(pair, repeat=True)

    def _drop_copies(self, pair: Pair) -> None:
        """Past the deadline the core reads each copy and says nothing."""
        while pair.copies:
            event = self._read(pair.replier, pair.copies.popleft())
            assert event.kind == EVENT["RF_EV_NONE"]

    def _forget(self, pair: Pair) -> None:
        self.now = max(self.now, DEADLINE + KEEP)
        assert pair.replier.poll(self.now).kind == EVENT["RF_EV_FORGOT"]
        assert pair.replier.poll(self.now).kind == EVENT["RF_EV_NONE"]
        self._drop_copies(pair)
        self.seen.add("a forgotten id")

    def agrees_with(self, s: World) -> None:
        assert self.outcome == s.outcome
        assert [p.received for p in self.pairs] == [st.received for st in s.streams]
        assert [p.runs for p in self.pairs] == [r.runs for r in s.repliers]
        assert [p.outcome in ("completed", "busy") for p in self.pairs] == [
            st.ended for st in s.streams
        ]
        assert self.late == s.late


def _replayable(s: World, name: str) -> bool:
    """Whether the C core can be driven through the action ``name`` from ``s``."""
    if name.endswith("_old"):
        return False
    if "_expire" in name:
        return s.past_deadline
    if name.endswith("_forget"):
        return s.past_deadline and s.repliers[int(name[1 : name.index("_")])].phase == "ended"
    return not (s.past_deadline and name.endswith(("_receive", "_replay")))


def _walk(library: ctypes.CDLL, rules: Rules, seed: int, seen: set[str]) -> None:
    choose = random.Random(seed)  # noqa: S311
    model = request_model(rules)
    assert isinstance(model.init, World)
    state: World = model.init
    run = Run(library, rules.repliers, seen)
    trace: list[str] = []
    while True:
        enabled = [a for a in model.actions if a.enabled(state) and _replayable(state, a.name)]
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
def test_the_c_core_replays_the_model(library: ctypes.CDLL, rules: Rules, walks: int) -> None:
    seen: set[str] = set()
    for seed in range(walks):
        _walk(library, rules, seed, seen)
    assert seen >= ALWAYS, ALWAYS - seen
