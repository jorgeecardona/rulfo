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

type Outcome = Literal["completed", "busy", "expired", "unroutable"]
type Phase = Literal["idle", "taken", "running", "ended", "hung", "crashed"]
type Redeliver = Literal["never", "to_dedup", "to_all"]
type Forget = Literal["after_every_deadline", "after_own_deadline", "when_not_running", "any_time"]
type Repeat = Literal["after_reconnect", "any_time"]
type Retry = Literal["never", "all_refused", "any_refused"]


@dataclass(frozen=True)
class Reply:
    """One reply frame. ``number`` counts the replies of one replier, from 0."""

    number: int


@dataclass(frozen=True)
class End:
    """The end frame of one replier. ``count`` is the number of replies it sent.

    ``busy`` means the replier refused the request.
    """

    count: int
    busy: bool = False


type Frame = Reply | End


@dataclass(frozen=True)
class Rules:
    """Which rules of ``request`` are on, and how many faults a run may have.

    The defaults are the design. Every other value exists for a test that shows a property fail.

    - ``redeliver``: who gets the request again. ``to_dedup`` sends it only when ``dedup`` is on.
    - ``repeat``: when a second copy can be sent. ``after_reconnect`` sends it only after the
      connection dropped. ``any_time`` also sends it on the same connection, while the first copy
      or its answer is still in flight.
    - ``refuses_repeats``: whether a replier may refuse a copy marked as sent again.
    - ``refuses_on_closed``: whether a replier that decides to refuse after the copy's connection
      closed still sends the busy end, on the new connection.
    - ``reads_old_connection``: whether the requester still reads the old connection after it sent
      the request again on a new one.
    - ``retry``: when a requester that got ``busy`` sends the request again: only when every
      replier refused, or when any replier did.

    - ``replays``: whether the program may send the replies again after a copy marked as sent
      again with a known id: all of them, then the end if it was sent. It can also do nothing.
    - ``takes_next_only``: whether the requester takes only the next reply number of a replier.

    A replier always looks up the id first: a copy with a known id never runs and is never
    refused.
    - ``forget``: when a replier may drop the id from its store. ``after_every_deadline`` is the
      design: the keep time ended, so every copy, also one read later, is past its deadline.
      ``after_own_deadline`` waits only for the deadline of the copy that ran.

    Each connection gives a copy its own deadline on the replier's clock: it can pass before or
    after the requester's deadline, and before or after the one of another connection. The model
    takes as given that the keep time covers the difference. The arithmetic is not modelled.
    """

    repliers: int = 1
    max_replies: int = 2
    deadline: bool = True
    detects_no_replier: bool = True
    dedup: bool = True
    redeliver: Redeliver = "to_dedup"
    forget: Forget = "after_every_deadline"
    checks_reply_count: bool = True
    drops_late_replies: bool = True
    may_hang: bool = True
    crashes: int = 1
    disconnects: int = 1
    redeliveries: int = 1
    repeat: Repeat = "after_reconnect"
    refusals: int = 1
    retry: Retry = "all_refused"
    refuses_repeats: bool = False
    refuses_on_closed: bool = False
    reads_old_connection: bool = False
    replays: bool = True
    takes_next_only: bool = True


@dataclass(frozen=True)
class Copy:
    """One copy of the request: whether it is marked as sent again, and its connection."""

    marked: bool
    conn: int


@dataclass(frozen=True)
class Replier:
    """One replier and the frames in flight on its connections.

    ``runs`` counts handler starts. ``conn`` numbers the current connection. ``inbox`` holds the
    copies of the request on it that the replier has not read: each is ``True`` when it is marked
    as sent again. ``old`` holds the unread copies of closed connections: a replier can still
    read them. ``outbox`` holds frames not yet received by the requester, in send order.

    In the phase ``taken`` the replier read a copy, may still refuse it, and has not started the
    handler. ``taken`` is that copy. ``all_past`` means that the keep time ended: every deadline
    passed. ``lost_end`` records that a connection drop lost the end of the handler. ``past`` holds the connections whose deadline passed. Only ``after_own_deadline``
    needs ``past`` and keeps ``taken`` after the start: the design drops the unread copies of a
    connection when its deadline passes, which is all that the deadline does there.
    """

    phase: Phase = "idle"
    remembers: bool = False
    runs: int = 0
    sent: int = 0
    conn: int = 0
    inbox: tuple[bool, ...] = ()
    old: tuple[Copy, ...] = ()
    outbox: tuple[Frame, ...] = ()
    taken: Copy | None = None
    past: frozenset[int] = frozenset()
    all_past: bool = False
    lost_end: bool = False


@dataclass(frozen=True)
class Stream:
    """What the requester has from one replier."""

    received: int = 0
    ended: bool = False
    refused: bool = False


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
    refusals: int = 0
    retries: int = 0


def _with_replier(s: World, i: int, r: Replier) -> World:
    return replace(s, repliers=(*s.repliers[:i], r, *s.repliers[i + 1 :]))


def _with_stream(s: World, i: int, st: Stream) -> World:
    return replace(s, streams=(*s.streams[:i], st, *s.streams[i + 1 :]))


def _send(rules: Rules) -> Callable[[World], World]:
    def apply(s: World) -> World:
        if not s.repliers:
            return replace(s, sent=True, outcome="unroutable" if rules.detects_no_replier else None)
        return replace(s, sent=True, repliers=tuple(replace(r, inbox=(False,)) for r in s.repliers))

    return apply


def _redeliver(s: World) -> World:
    repliers = tuple(
        r if st.ended else replace(r, inbox=(*r.inbox, True))
        for r, st in zip(s.repliers, s.streams, strict=True)
    )
    return replace(s, repliers=repliers, redeliveries=s.redeliveries + 1)


def _is_past(r: Replier, conn: int) -> bool:
    return r.all_past or conn in r.past


def _started(rules: Rules, r: Replier, copy: Copy) -> Replier:
    """``r`` after it started the handler for ``copy``."""
    ran = copy if rules.forget == "after_own_deadline" else None
    return replace(r, phase="running", remembers=True, runs=r.runs + 1, taken=ran)


def _receive(rules: Rules, i: int, *, old: bool = False) -> Callable[[World], World]:
    def apply(s: World) -> World:
        r = s.repliers[i]
        if old:
            copy, r = r.old[0], replace(r, old=r.old[1:])
        else:
            copy, r = Copy(r.inbox[0], r.conn), replace(r, inbox=r.inbox[1:])
        repeated = rules.dedup and r.remembers
        if _is_past(r, copy.conn) or repeated:
            return _with_replier(s, i, r)
        if r.phase in ("taken", "running"):
            # Only a replier that does not drop repeats gets here: it starts a second handler.
            return _with_replier(s, i, replace(r, runs=r.runs + 1))
        # Without a refusal left, taking and starting in one step reaches the same outcomes.
        if s.refusals < rules.refusals and (rules.refuses_repeats or not copy.marked):
            return _with_replier(s, i, replace(r, phase="taken", remembers=True, taken=copy))
        return _with_replier(s, i, _started(rules, r, copy))

    return apply


def _start(rules: Rules, i: int) -> Callable[[World], World]:
    def apply(s: World) -> World:
        r = s.repliers[i]
        return _with_replier(s, i, _started(rules, r, _taken(r)))

    return apply


def _may_replay(rules: Rules, r: Replier, copy: Copy) -> bool:
    """Whether the program can send replies again for ``copy``, and there is something to send."""
    has_sent = r.sent > 0 or r.phase == "ended"
    known = rules.dedup and r.remembers and not _is_past(r, copy.conn)
    return rules.replays and copy.marked and known and has_sent


def _replay(i: int, *, old: bool = False) -> Callable[[World], World]:
    def apply(s: World) -> World:
        r = s.repliers[i]
        r = replace(r, old=r.old[1:]) if old else replace(r, inbox=r.inbox[1:])
        again: tuple[Frame, ...] = tuple(Reply(number) for number in range(r.sent))
        if r.phase == "ended":
            again = (*again, End(r.sent))
        return _with_replier(s, i, replace(r, outbox=(*r.outbox, *again)))

    return apply


def _refuse(rules: Rules, i: int) -> Callable[[World], World]:
    def apply(s: World) -> World:
        r = s.repliers[i]
        answers = _taken(r).conn == r.conn or rules.refuses_on_closed
        outbox = (*r.outbox, End(0, busy=True)) if answers else r.outbox
        refused = replace(r, phase="idle", remembers=False, taken=None, outbox=outbox)
        return replace(_with_replier(s, i, refused), refusals=s.refusals + 1)

    return apply


def _resends(rules: Rules) -> bool:
    return rules.redeliver == "to_all" or (rules.redeliver == "to_dedup" and rules.dedup)


def _disconnect(rules: Rules, i: int) -> Callable[[World], World]:
    def apply(s: World) -> World:
        r = s.repliers[i]
        again = (
            rules.repeat == "after_reconnect"
            and _resends(rules)
            and s.outcome is None
            and not s.streams[i].ended
            and s.redeliveries < rules.redeliveries
        )
        lost = not rules.reads_old_connection and any(
            isinstance(f, End) and not f.busy for f in r.outbox
        )
        r = replace(
            r,
            lost_end=r.lost_end or lost,
            conn=r.conn + 1,
            old=(*r.old, *(Copy(marked, r.conn) for marked in r.inbox)),
            inbox=(True,) if again else (),
            outbox=r.outbox if rules.reads_old_connection else (),
        )
        return replace(
            _with_replier(s, i, r),
            disconnects=s.disconnects + 1,
            redeliveries=s.redeliveries + 1 if again else s.redeliveries,
        )

    return apply


def _retry(s: World) -> World:
    """The requester sends a refused request again, with the time that is left."""
    return replace(
        s,
        outcome=None,
        retries=s.retries + 1,
        repliers=tuple(replace(r, inbox=(*r.inbox, False)) for r in s.repliers),
        streams=(Stream(),) * len(s.streams),
    )


def _may_retry(rules: Rules, s: World) -> bool:
    if s.outcome != "busy" or s.past_deadline or s.retries >= 1:
        return False
    return rules.retry == "any_refused" or all(st.refused for st in s.streams)


def _taken(r: Replier) -> Copy:
    if r.taken is None:
        msg = "this replier took no copy"
        raise ValueError(msg)
    return r.taken


def _reply(i: int) -> Callable[[World], World]:
    def apply(s: World) -> World:
        r = s.repliers[i]
        outbox = (*r.outbox, Reply(r.sent))
        return _with_replier(s, i, replace(r, sent=r.sent + 1, outbox=outbox))

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
            counts_late = isinstance(head, Reply) and not rules.drops_late_replies
            return replace(s, late=s.late + 1) if counts_late else s
        if isinstance(head, End):
            if rules.checks_reply_count and not head.busy and head.count != st.received:
                return s
            s = _with_stream(s, i, replace(st, ended=True, refused=head.busy))
            if not all(x.ended for x in s.streams):
                return s
            refused = any(x.refused for x in s.streams)
            return replace(s, outcome="busy" if refused else "completed")
        if rules.takes_next_only and head.number != st.received:
            return s
        return _with_stream(s, i, replace(st, received=st.received + 1))

    return apply


def _may_forget(rules: Rules, r: Replier) -> bool:
    if not r.remembers or rules.forget == "any_time":
        return r.remembers
    if r.phase in ("taken", "running", "hung"):
        return False
    if rules.forget == "when_not_running":
        return True
    if rules.forget == "after_own_deadline":
        return r.taken is not None and _is_past(r, r.taken.conn)
    return rules.deadline


def _forget(rules: Rules, i: int) -> Callable[[World], World]:
    def apply(s: World) -> World:
        r = replace(s.repliers[i], remembers=False)
        if rules.forget == "after_every_deadline":
            r = replace(r, all_past=True, inbox=(), old=())
        return _with_replier(s, i, r)

    return apply


def _may_expire(rules: Rules, r: Replier, conn: int) -> bool:
    """Whether the deadline of ``conn`` can pass now and change what the replier does."""
    if not rules.deadline or _is_past(r, conn):
        return False
    unread = (conn == r.conn and bool(r.inbox)) or any(c.conn == conn for c in r.old)
    ran = r.remembers and r.taken is not None and r.taken.conn == conn
    return unread or (rules.forget == "after_own_deadline" and ran)


def _expire(rules: Rules, i: int, conn: int) -> Callable[[World], World]:
    def apply(s: World) -> World:
        r = s.repliers[i]
        r = replace(
            r,
            inbox=() if conn == r.conn else r.inbox,
            old=tuple(c for c in r.old if c.conn != conn),
        )
        if rules.forget == "after_own_deadline":
            r = replace(r, past=r.past | {conn})
        return _with_replier(s, i, r)

    return apply


def _replier_actions(rules: Rules, i: int) -> list[Action[World]]:
    def at(s: World) -> Replier:
        return s.repliers[i]

    actions: list[Action[World]] = [
        Action(
            f"R{i}_receive",
            lambda s: bool(at(s).inbox) and at(s).phase not in ("hung", "crashed"),
            _receive(rules, i),
            fair="weak",
        ),
        Action(
            f"R{i}_receive_old",
            lambda s: bool(at(s).old) and at(s).phase not in ("hung", "crashed"),
            _receive(rules, i, old=True),
        ),
        # The program sends its replies again instead of doing nothing with the copy.
        Action(
            f"R{i}_replay",
            lambda s: (
                bool(at(s).inbox)
                and at(s).phase not in ("hung", "crashed")
                and _may_replay(rules, at(s), Copy(at(s).inbox[0], at(s).conn))
            ),
            _replay(i),
        ),
        Action(
            f"R{i}_replay_old",
            lambda s: (
                bool(at(s).old)
                and at(s).phase not in ("hung", "crashed")
                and _may_replay(rules, at(s), at(s).old[0])
            ),
            _replay(i, old=True),
        ),
        Action(f"R{i}_start", lambda s: at(s).phase == "taken", _start(rules, i), fair="weak"),
        # The store of ids is full, or the deadline is longer than the replier accepts.
        Action(
            f"R{i}_refuse",
            lambda s: (
                s.refusals < rules.refusals
                and at(s).phase == "taken"
                and (rules.refuses_repeats or not _taken(at(s)).marked)
            ),
            _refuse(rules, i),
        ),
        Action(
            f"R{i}_reply",
            lambda s: at(s).phase == "running" and at(s).sent < rules.max_replies,
            _reply(i),
        ),
        Action(f"R{i}_end", lambda s: at(s).phase == "running", _end(i), fair="weak"),
        Action(f"R{i}_deliver", lambda s: bool(at(s).outbox), _deliver(rules, i), fair="weak"),
        Action(f"R{i}_forget", lambda s: _may_forget(rules, at(s)), _forget(rules, i)),
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
            lambda s: (
                s.disconnects < rules.disconnects
                and s.sent
                and (s.outcome is None or bool(at(s).inbox) or bool(at(s).outbox))
            ),
            _disconnect(rules, i),
        ),
    ]
    actions.extend(
        Action(
            f"R{i}_expire{conn}",
            lambda s, conn=conn: _may_expire(rules, at(s), conn),
            _expire(rules, i, conn),
        )
        for conn in range(rules.disconnects + 1)
    )
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


def _completed_has_every_reply(s: World) -> bool:
    if s.outcome != "completed":
        return True
    pairs = zip(s.repliers, s.streams, strict=True)
    return all(st.ended and st.received == r.sent for r, st in pairs)


def _refused_did_not_run(s: World) -> bool:
    if s.outcome != "busy":
        return True
    pairs = zip(s.repliers, s.streams, strict=True)
    return all(r.runs == 0 for r, st in pairs if st.refused)


def _no_reply_twice(s: World) -> bool:
    return all(st.received <= r.sent for r, st in zip(s.repliers, s.streams, strict=True))


def _no_reply_after_the_end(s: World) -> bool:
    return s.late == 0


def _unroutable_iff_no_replier(s: World) -> bool:
    if s.outcome is None:
        return True
    return (s.outcome == "unroutable") == (not s.repliers)


def request_model(rules: Rules | None = None) -> Model[World]:
    """One request under ``rules``. ``Rules()`` is the design in ``docs/operations.md``."""
    rules = rules or Rules()
    actions: list[Action[World]] = [
        Action("send", lambda s: not s.sent, _send(rules), fair="weak"),
    ]
    if rules.repeat == "any_time" and _resends(rules):
        actions.append(
            Action(
                "redeliver",
                lambda s: s.sent and s.outcome is None and s.redeliveries < rules.redeliveries,
                _redeliver,
            )
        )
    if rules.retry != "never":
        actions.append(Action("retry", lambda s: _may_retry(rules, s), _retry))
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
            "completed-has-every-reply": _completed_has_every_reply,
            "refused-did-not-run": _refused_did_not_run,
            "no-reply-twice": _no_reply_twice,
            "no-reply-after-the-end": _no_reply_after_the_end,
            "unroutable-iff-no-replier": _unroutable_iff_no_replier,
        },
        terminal=lambda s: s.outcome is not None,
    )


def check_safety(rules: Rules | None = None) -> Result[World]:
    """Check the invariants in every reachable state."""
    return check(request_model(rules))


def check_ends(rules: Rules | None = None) -> LivenessResult[World]:
    """Check that every sent request gets an outcome, the first time and after a retry."""
    return check_liveness(
        request_model(rules),
        goal=lambda s: s.outcome is not None,
        goal_name="the request has an outcome",
        leadsto_from=lambda s: s.sent and s.outcome is None,
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
