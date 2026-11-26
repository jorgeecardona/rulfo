"""Model of the ``request`` operation: one request, N repliers and a deadline.

The rules are in ``docs/operations.md``. ``Rules`` turns each rule on or off, so a test can show
that a property holds with the rule and fails without it.

What the model leaves out is listed in ``docs/model.md``.

Run: python models/request_reply.py
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from musil import Action, LivenessResult, Model, Result, check, check_liveness

type Outcome = Literal["replied", "expired", "unroutable"]
type Phase = Literal["idle", "running", "ended", "hung", "crashed"]
type Redeliver = Literal["never", "to_dedup", "to_all"]
type Forget = Literal["after_deadline", "when_not_running", "any_time"]

REPLY: Literal["reply"] = "reply"


@dataclass(frozen=True)
class End:
    """The end frame of one replier. ``count`` is the number of replies it sent."""

    count: int


type Frame = Literal["reply"] | End


@dataclass(frozen=True)
class Rules:
    """Which rules of ``request`` are on, and how many faults a run may have.

    The defaults are the design. Every other value exists for a test that shows a property fail.

    - ``redeliver``: who gets the request again. ``to_dedup`` sends it only when ``dedup`` is on.
    - ``forget``: when a replier may drop the id from its store. A store has a fixed size, so
      other requests push ids out. ``after_deadline`` is the only safe value.
    """

    repliers: int = 1
    max_replies: int = 2
    deadline: bool = True
    detects_no_replier: bool = True
    dedup: bool = True
    redeliver: Redeliver = "to_dedup"
    forget: Forget = "after_deadline"
    checks_reply_count: bool = True
    drops_late_replies: bool = True
    may_hang: bool = True
    crashes: int = 1
    disconnects: int = 1
    redeliveries: int = 1


@dataclass(frozen=True)
class Replier:
    """One replier and the frames in flight on its connection.

    ``runs`` counts handler starts. ``inbox`` counts copies of the request not yet received.
    ``outbox`` holds frames not yet received by the requester, in send order.
    """

    phase: Phase = "idle"
    remembers: bool = False
    runs: int = 0
    sent: int = 0
    inbox: int = 0
    outbox: tuple[Frame, ...] = ()


@dataclass(frozen=True)
class Stream:
    """What the requester has from one replier."""

    received: int = 0
    ended: bool = False


@dataclass(frozen=True)
class World:
    """The requester, the repliers and the fault counters.

    ``late`` counts replies handed to the caller after the outcome. It must stay 0.
    """

    repliers: tuple[Replier, ...]
    streams: tuple[Stream, ...]
    sent: bool = False
    outcome: Outcome | None = None
    past_deadline: bool = False
    late: int = 0
    crashes: int = 0
    disconnects: int = 0
    redeliveries: int = 0


def _with_replier(s: World, i: int, r: Replier) -> World:
    return replace(s, repliers=(*s.repliers[:i], r, *s.repliers[i + 1 :]))


def _with_stream(s: World, i: int, st: Stream) -> World:
    return replace(s, streams=(*s.streams[:i], st, *s.streams[i + 1 :]))


def _send(rules: Rules) -> Callable[[World], World]:
    def apply(s: World) -> World:
        if not s.repliers:
            return replace(s, sent=True, outcome="unroutable" if rules.detects_no_replier else None)
        return replace(s, sent=True, repliers=tuple(replace(r, inbox=1) for r in s.repliers))

    return apply


def _redeliver(s: World) -> World:
    repliers = tuple(
        r if st.ended else replace(r, inbox=r.inbox + 1)
        for r, st in zip(s.repliers, s.streams, strict=True)
    )
    return replace(s, repliers=repliers, redeliveries=s.redeliveries + 1)


def _receive(rules: Rules, i: int) -> Callable[[World], World]:
    def apply(s: World) -> World:
        r = replace(s.repliers[i], inbox=s.repliers[i].inbox - 1)
        repeated = rules.dedup and r.remembers
        if s.past_deadline or repeated:
            return _with_replier(s, i, r)
        return _with_replier(s, i, replace(r, phase="running", remembers=True, runs=r.runs + 1))

    return apply


def _reply(i: int) -> Callable[[World], World]:
    def apply(s: World) -> World:
        r = s.repliers[i]
        return _with_replier(s, i, replace(r, sent=r.sent + 1, outbox=(*r.outbox, REPLY)))

    return apply


def _end(i: int) -> Callable[[World], World]:
    def apply(s: World) -> World:
        r = s.repliers[i]
        return _with_replier(s, i, replace(r, phase="ended", outbox=(*r.outbox, End(r.sent))))

    return apply


def _deliver(rules: Rules, i: int) -> Callable[[World], World]:
    def apply(s: World) -> World:
        r, st = s.repliers[i], s.streams[i]
        head = r.outbox[0]
        s = _with_replier(s, i, replace(r, outbox=r.outbox[1:]))
        if s.outcome is not None:
            counts_late = head == REPLY and not rules.drops_late_replies
            return replace(s, late=s.late + 1) if counts_late else s
        if isinstance(head, End):
            if rules.checks_reply_count and head.count != st.received:
                return s
            s = _with_stream(s, i, replace(st, ended=True))
            return replace(s, outcome="replied") if all(x.ended for x in s.streams) else s
        return _with_stream(s, i, replace(st, received=st.received + 1))

    return apply


def _may_forget(rules: Rules, s: World, r: Replier) -> bool:
    if not r.remembers:
        return False
    if rules.forget == "any_time":
        return True
    handler_is_done = r.phase not in ("running", "hung")
    if rules.forget == "when_not_running":
        return handler_is_done
    return handler_is_done and s.past_deadline


def _replier_actions(rules: Rules, i: int) -> list[Action[World]]:
    def at(s: World) -> Replier:
        return s.repliers[i]

    actions: list[Action[World]] = [
        Action(
            f"R{i}_receive",
            lambda s: at(s).inbox > 0 and at(s).phase not in ("hung", "crashed"),
            _receive(rules, i),
            fair="weak",
        ),
        Action(
            f"R{i}_reply",
            lambda s: at(s).phase == "running" and at(s).sent < rules.max_replies,
            _reply(i),
        ),
        Action(f"R{i}_end", lambda s: at(s).phase == "running", _end(i), fair="weak"),
        Action(f"R{i}_deliver", lambda s: bool(at(s).outbox), _deliver(rules, i), fair="weak"),
        Action(
            f"R{i}_forget",
            lambda s: _may_forget(rules, s, at(s)),
            lambda s: _with_replier(s, i, replace(at(s), remembers=False)),
        ),
        Action(
            f"R{i}_crash",
            lambda s: s.crashes < rules.crashes and at(s).phase != "crashed",
            lambda s: replace(
                _with_replier(s, i, replace(at(s), phase="crashed")), crashes=s.crashes + 1
            ),
        ),
        # The connection drops and comes back: every frame in flight on it is lost.
        Action(
            f"R{i}_disconnect",
            lambda s: s.disconnects < rules.disconnects and (at(s).inbox > 0 or bool(at(s).outbox)),
            lambda s: replace(
                _with_replier(s, i, replace(at(s), inbox=0, outbox=())),
                disconnects=s.disconnects + 1,
            ),
        ),
    ]
    if rules.may_hang:
        # A handler that never ends. A dead state, because a run with an enabled action goes on.
        actions.append(
            Action(
                f"R{i}_hang",
                lambda s: at(s).phase == "running",
                lambda s: _with_replier(s, i, replace(at(s), phase="hung")),
            )
        )
    return actions


def _runs_at_most_once(s: World) -> bool:
    return all(r.runs <= 1 for r in s.repliers)


def _replied_is_complete(s: World) -> bool:
    if s.outcome != "replied":
        return True
    pairs = zip(s.repliers, s.streams, strict=True)
    return all(st.ended and st.received == r.sent for r, st in pairs)


def _no_reply_after_the_end(s: World) -> bool:
    return s.late == 0


def _unroutable_iff_no_replier(s: World) -> bool:
    if s.outcome is None:
        return True
    return (s.outcome == "unroutable") == (not s.repliers)


def request_model(rules: Rules | None = None) -> Model[World]:
    """One request under ``rules``. ``Rules()`` is the design in ``docs/operations.md``."""
    rules = rules or Rules()
    resends = rules.redeliver == "to_all" or (rules.redeliver == "to_dedup" and rules.dedup)
    actions: list[Action[World]] = [
        Action("send", lambda s: not s.sent, _send(rules), fair="weak"),
        Action(
            "redeliver",
            lambda s: (
                resends and s.sent and s.outcome is None and s.redeliveries < rules.redeliveries
            ),
            _redeliver,
        ),
    ]
    if rules.deadline:
        actions.append(
            Action(
                "deadline",
                lambda s: s.sent and not s.past_deadline,
                lambda s: replace(s, past_deadline=True, outcome=s.outcome or "expired"),
                fair="weak",
            )
        )
    for i in range(rules.repliers):
        actions += _replier_actions(rules, i)
    return Model(
        init=World(
            repliers=(Replier(),) * rules.repliers,
            streams=(Stream(),) * rules.repliers,
        ),
        actions=actions,
        invariants={
            "runs-at-most-once": _runs_at_most_once,
            "replied-is-complete": _replied_is_complete,
            "no-reply-after-the-end": _no_reply_after_the_end,
            "unroutable-iff-no-replier": _unroutable_iff_no_replier,
        },
        terminal=lambda s: s.outcome is not None,
    )


def check_safety(rules: Rules | None = None) -> Result[World]:
    """Check the four invariants in every reachable state."""
    return check(request_model(rules))


def check_ends(rules: Rules | None = None) -> LivenessResult[World]:
    """Check that every run gives the requester an outcome."""
    return check_liveness(
        request_model(rules),
        goal=lambda s: s.outcome is not None,
        goal_name="the request has an outcome",
    )


def main() -> int:
    print("=== the design, one replier ===")
    print(check_safety())
    print(check_ends())

    print("\n=== no replier ===")
    print(check_safety(Rules(repliers=0)))

    print("\n=== no deadline: a hung handler ===")
    print(check_ends(Rules(deadline=False, crashes=0, disconnects=0)))

    print("\n=== a replier that forgets the id when its handler is done ===")
    print(check_safety(Rules(forget="when_not_running")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
