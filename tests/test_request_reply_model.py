"""The request model: each property holds under the design and fails without its rule."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from musil import assert_ok, assert_violates, explore, witness

from request_reply import Rules, World, check_ends, check_safety, request_model

NO_FAULTS = Rules(may_hang=False, crashes=0, disconnects=0)


def _reachable(rules: Rules, predicate: Callable[[World], bool]) -> bool:
    return witness(explore(request_model(rules)), predicate) is not None


@pytest.mark.parametrize(
    "rules",
    [
        Rules(),
        Rules(repliers=0),
        Rules(repliers=2, max_replies=1),
    ],
    ids=["one-replier", "no-replier", "two-repliers"],
)
def test_the_design_keeps_every_invariant_and_always_ends(rules: Rules) -> None:
    assert_ok(check_safety(rules))
    assert_ok(check_ends(rules))


def test_a_request_can_end_completed_with_zero_replies() -> None:
    assert _reachable(Rules(), lambda s: s.outcome == "completed" and s.streams[0].received == 0)


def test_a_request_can_end_completed_with_many_replies() -> None:
    assert _reachable(Rules(), lambda s: s.outcome == "completed" and s.streams[0].received == 2)


def test_two_repliers_can_both_reply() -> None:
    assert _reachable(
        Rules(repliers=2, max_replies=1),
        lambda s: s.outcome == "completed" and all(st.received == 1 for st in s.streams),
    )


def test_a_crashed_replier_gives_expired_and_keeps_the_replies_that_arrived() -> None:
    assert _reachable(
        Rules(),
        lambda s: (
            s.outcome == "expired"
            and s.repliers[0].phase == "crashed"
            and s.streams[0].received == 1
        ),
    )


def test_one_dead_replier_of_two_gives_expired_not_completed() -> None:
    rules = Rules(repliers=2, max_replies=1)
    dead_before_end = _reachable(
        rules,
        lambda s: (
            s.outcome == "completed"
            and any(
                r.phase in ("hung", "crashed") and not st.ended
                for r, st in zip(s.repliers, s.streams, strict=True)
            )
        ),
    )
    assert not dead_before_end


def test_no_replier_is_unroutable_and_never_waits_for_the_deadline() -> None:
    assert _reachable(Rules(repliers=0), lambda s: s.outcome == "unroutable")
    assert not _reachable(Rules(repliers=0), lambda s: s.outcome == "expired")
    assert_violates(
        check_safety(Rules(repliers=0, detects_no_replier=False)),
        invariant="unroutable-iff-no-replier",
    )


@pytest.mark.parametrize(
    "rules",
    [
        Rules(deadline=False, crashes=0, disconnects=0),
        Rules(deadline=False, may_hang=False, disconnects=0),
        Rules(deadline=False, may_hang=False, crashes=0),
    ],
    ids=["hung-handler", "crashed-replier", "lost-frame"],
)
def test_without_a_deadline_one_fault_leaves_the_request_open(rules: Rules) -> None:
    assert not check_ends(rules).ok


def test_without_a_deadline_and_without_faults_the_request_still_ends() -> None:
    assert_ok(check_ends(Rules(deadline=False, may_hang=False, crashes=0, disconnects=0)))


def test_a_repeated_request_runs_twice_on_a_replier_that_does_not_dedup() -> None:
    assert_violates(
        check_safety(Rules(dedup=False, redeliver="to_all")), invariant="runs-at-most-once"
    )


def test_a_replier_that_does_not_dedup_gets_no_repeat() -> None:
    assert_ok(check_safety(Rules(dedup=False)))


@pytest.mark.parametrize(
    "rules",
    [Rules(forget="when_not_running"), Rules(forget="any_time")],
    ids=["when-not-running", "any-time"],
)
def test_forgetting_the_id_before_the_deadline_runs_the_request_twice(rules: Rules) -> None:
    assert_violates(check_safety(rules), invariant="runs-at-most-once", via=["R0_forget"])


def test_an_end_without_the_reply_count_hides_a_lost_reply() -> None:
    assert_violates(
        check_safety(Rules(checks_reply_count=False)),
        invariant="completed-has-every-reply",
        via=["R0_disconnect"],
    )
    assert_ok(check_safety(Rules(checks_reply_count=False, disconnects=0)))


def test_a_reply_after_the_outcome_reaches_the_caller_unless_dropped() -> None:
    assert_violates(
        check_safety(Rules(drops_late_replies=False)), invariant="no-reply-after-the-end"
    )


def test_the_fault_free_model_is_a_strict_subset() -> None:
    assert_ok(check_safety(NO_FAULTS))
    assert not _reachable(NO_FAULTS, lambda s: s.outcome == "expired" and s.streams[0].ended)


def test_a_replier_that_refuses_gives_busy_and_runs_nothing() -> None:
    assert _reachable(Rules(), lambda s: s.outcome == "busy" and s.repliers[0].runs == 0)
    assert not _reachable(Rules(), lambda s: s.outcome == "busy" and s.repliers[0].runs > 0)


def test_without_a_refusal_there_is_no_busy() -> None:
    assert not _reachable(Rules(refusals=0), lambda s: s.outcome == "busy")


def test_a_second_copy_on_the_same_connection_makes_busy_wrong() -> None:
    assert_violates(
        check_safety(Rules(repeat="any_time")),
        invariant="refused-did-not-run",
        via=["R0_refuse", "redeliver"],
    )


def test_a_second_copy_at_any_time_still_runs_at_most_once() -> None:
    result = check_safety(Rules(repeat="any_time", refusals=0))
    assert_ok(result)


def test_a_retry_after_busy_can_complete() -> None:
    assert _reachable(Rules(), lambda s: s.retries == 1 and s.outcome == "completed")


def test_a_retry_with_a_new_id_is_safe_with_one_replier() -> None:
    assert_ok(check_safety(Rules(retry="new_id")))


def test_a_retry_with_a_new_id_runs_twice_with_two_repliers() -> None:
    assert_violates(
        check_safety(Rules(repliers=2, max_replies=1, retry="new_id")),
        invariant="runs-at-most-once",
        via=["retry"],
    )


def test_a_retry_to_two_repliers_without_dedup_runs_twice() -> None:
    assert_violates(
        check_safety(Rules(repliers=2, max_replies=1, dedup=False)),
        invariant="runs-at-most-once",
        via=["retry"],
    )


def test_a_retry_to_two_repliers_can_expire_with_one_that_already_ran_it() -> None:
    assert _reachable(
        Rules(repliers=2, max_replies=1, may_hang=False, crashes=0, disconnects=0),
        lambda s: s.retries == 1 and s.outcome == "expired" and s.repliers[1].runs == 1,
    )
