"""What each recorded fact counts for (``budgets/records.py``)."""

from __future__ import annotations

from aksha.runtime.budgets import records
from tests.support.budgets import Log


def test_a_continue_charges_one_turn_per_instance_and_turn() -> None:
    log = Log()
    a = log.decided("continue", turn=1)
    b = log.decided("continue", turn=1)
    c = log.decided("continue", turn=1, instance="b")
    assert records.charged_turn(a) == records.charged_turn(b)
    assert records.charged_turn(a) != records.charged_turn(c)


def test_a_continue_without_a_turn_is_its_own_turn() -> None:
    log = Log()
    assert records.charged_turn(log.decided()) != records.charged_turn(log.decided())


def test_hold_stop_and_resume_records_charge_nothing() -> None:
    log = Log()
    assert records.charged_turn(log.decided("hold")) is None
    assert records.charged_turn(log.decided("stop")) is None
    assert records.charged_turn(log.resumed()) is None
    assert records.charged_turn(log.decided("continue", resumes_turn=True)) is None
    assert records.charged_turn(log.decided("continue", cause="resume")) is None


def test_a_veto_is_a_finish_turned_into_a_continue() -> None:
    log = Log()
    assert records.is_veto(log.decided("continue", candidate="finish"))
    assert not records.is_veto(log.decided("continue", candidate="turn_end"))
    assert not records.is_veto(log.decided("stop", candidate="finish"))


def test_progress_is_a_completed_tool_step_or_a_saved_artifact() -> None:
    log = Log()
    assert records.is_progress(log.completed("s1"))
    assert records.is_progress(log.saved())
    assert not records.is_progress(log.completed("s2", outcome="error"))
    assert not records.is_progress(log.completed("s3", kind="model"))
    assert not records.is_progress(log.tool("s4"))


def test_active_time_is_durations_plus_the_backoff_of_a_retry() -> None:
    log = Log()
    assert records.active_ms(log.failed("s", 1, duration=100, backoff=500)) == 600
    assert records.active_ms(log.failed("s", 2, duration=100)) == 100
    assert records.active_ms(log.completed("s", duration=40)) == 40
    assert records.active_ms(log.tool("s")) == 0


def test_usage_is_read_from_model_steps_and_a_tool_bodys_model_calls() -> None:
    log = Log()
    use = {"input_tokens": 10, "output_tokens": 5, "cost_micro_usd": 70}
    assert records.usage(log.completed("m", kind="model", usage=use)) == (15, 70)
    assert records.usage(log.completed("t", kind="tool", usage=use)) == (0, 0)
    assert records.usage(log.completed("m2", kind="model")) == (0, 0)
    calls = [{"alias": "a", "usage": use}, {"alias": "b", "usage": {"input_tokens": 3}}]
    body = log.add("step.completed", {"outcome": "ok", "kind": "tool",
                                      "value": {"model_calls": calls}}, step="t2")  # fmt: skip
    assert records.usage(body) == (18, 70)


def test_automatic_starts_are_resumes_rebuilds_continuations_and_timers() -> None:
    log = Log()
    assert records.is_automatic_start(log.resumed())
    assert records.is_automatic_start(log.decided("continue", cause="timer"))
    assert records.is_automatic_start(log.add("run.started", {"agent": "a", "cause": "rebuild"}))
    assert records.is_automatic_start(log.add("run.started", {"agent": "a", "cause": "timer"}))
    assert not records.is_automatic_start(log.started())
    assert not records.is_automatic_start(log.decided("continue", cause="command:send"))
