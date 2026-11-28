"""The limits inside a run and the attempt count (ADR-P14 D3, D7), each a projection."""

from __future__ import annotations

from datetime import timedelta

from aksha.runtime.budgets.limits import (
    attempt_number,
    auto_start_allowed,
    automatic_starts,
    limit_reached,
    resumes_since_progress,
    turns_since_entry,
    turns_used,
    vetoes_since_entry,
)
from aksha.runtime.budgets.types import LimitHit, Limits
from tests.support.budgets import RUN, Log
from tests.support.events import START


def test_attempt_number_is_one_more_than_the_recorded_attempts() -> None:
    log = Log()
    assert attempt_number(log.view(), "R-0001/a/t01/c01") == 1
    log.failed("R-0001/a/t01/c01", 1)
    log.failed("R-0001/a/t01/c01", 2)
    log.failed("R-0001/a/t01/c02", 1)
    log.add("attempt.discarded", {"attempt": 2, "result_hash": "x"}, step="R-0001/a/t01/c02")
    assert attempt_number(log.view(), "R-0001/a/t01/c01") == 3
    assert attempt_number(log.view(), "R-0001/a/t01/c02") == 3


def test_turn_limit_fail_at_11() -> None:
    log = Log()
    log.started()
    for turn in range(1, 11):
        assert limit_reached(log.view(), RUN, "a", Limits()) == ()
        log.decided("continue", turn=turn)
    assert turns_used(log.view(), RUN, "a") == 10
    assert limit_reached(log.view(), RUN, "a", Limits()) == (LimitHit("turn_limit", 10, 10, "a"),)
    assert limit_reached(log.view(), RUN, "b", Limits()) == ()


def test_the_third_refusal_is_blocked() -> None:
    log = Log()
    log.entered()
    # Each check asks about the next refusal: two are given back, the third fails.
    assert limit_reached(log.view(), RUN, "a", Limits(turn_limit=99)) == ()
    log.decided("continue", candidate="finish")
    assert limit_reached(log.view(), RUN, "a", Limits(turn_limit=99)) == ()
    log.decided("continue", candidate="finish")
    hits = limit_reached(log.view(), RUN, "a", Limits(turn_limit=99))
    assert hits == (LimitHit("stop_veto_limit", 3, 3, "a"),)


def test_the_veto_count_starts_again_on_each_entry() -> None:
    log = Log()
    log.entered()
    for _ in range(3):
        log.decided("continue", candidate="finish")
    log.entered()
    log.decided("continue", candidate="finish")
    assert vetoes_since_entry(log.view(), RUN, "a") == 1


def test_the_turn_count_starts_again_on_a_rebuild_entry() -> None:
    # A rebuild re-enters the instance: its turns so far belong to the version it replaces.
    log = Log()
    log.entered()
    for turn in range(1, 5):
        log.decided("continue", turn=turn)
    log.entered()
    for turn in range(5, 9):
        log.decided("continue", turn=turn)
    assert turns_used(log.view(), RUN, "a") == 8  # turn numbers still run over the life
    assert turns_since_entry(log.view(), RUN, "a") == 4
    assert limit_reached(log.view(), RUN, "a", Limits(turn_limit=6)) == ()
    log.decided("continue", turn=9)
    log.decided("continue", turn=10)
    hit = LimitHit("turn_limit", 6, 6, "a")
    assert limit_reached(log.view(), RUN, "a", Limits(turn_limit=6)) == (hit,)


def test_restart_limit_from_log() -> None:
    log = Log()
    log.started()
    for _ in range(3):
        log.resumed()
    assert resumes_since_progress(log.view(), RUN) == 3
    assert limit_reached(log.view(), RUN, None, Limits()) == (LimitHit("restart_limit", 3, 3),)
    log.completed("R-0001/a/t01/c01")  # progress resets the count
    assert limit_reached(log.view(), RUN, None, Limits()) == ()
    log.resumed()
    log.saved()
    assert resumes_since_progress(log.view(), RUN) == 0


def test_a_failed_tool_step_is_not_progress() -> None:
    log = Log()
    for _ in range(2):
        log.resumed()
        log.completed("s", outcome="error")
    assert resumes_since_progress(log.view(), RUN) == 2


def test_automatic_starts_are_counted_in_the_window_from_recorded_times() -> None:
    log = Log()
    log.started()
    log.add("stop.decided", {"walk": 1, "verdict": "continue", "cause": "resume"}, at_s=10)
    log.add("run.started", {"agent": "a", "cause": "rebuild"}, at_s=60)
    log.add("stop.decided", {"walk": 2, "verdict": "continue", "cause": "continuation"}, at_s=100)
    now = START + timedelta(seconds=120)
    limits = Limits(auto_starts=(3, 300_000))
    assert automatic_starts(log.view(), RUN, now, 300_000) == 3
    assert not auto_start_allowed(log.view(), RUN, limits, now)
    later = START + timedelta(seconds=320)  # the first start has left the window
    assert automatic_starts(log.view(), RUN, later, 300_000) == 2
    assert auto_start_allowed(log.view(), RUN, limits, later)


def test_a_completion_check_reset_starts_the_veto_count_again() -> None:
    log = Log()
    log.entered()
    for _ in range(3):
        log.decided("continue", candidate="finish")
    assert vetoes_since_entry(log.view(), RUN, "a") == 3
    log.add("completion_check.reset", {"cause": "answer"}, instance="a")
    assert vetoes_since_entry(log.view(), RUN, "a") == 0
