"""Stop-policy evaluation (ADR-P16 D3): one record per candidate, the precedence, the limits."""

from __future__ import annotations

from typing import Any

import pytest

from aksha.runtime.budgets.limits import turns_used, vetoes_since_entry
from aksha.runtime.budgets.types import Budget
from aksha.runtime.stop_policy.errors import StopRefused
from aksha.runtime.stop_policy.evaluation import evaluate_stop
from aksha.runtime.stop_policy.types import StopCandidate, Verdict
from aksha.runtime.tasks.status import statuses
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import CheckResult, InstanceRef, Task
from tests.support.budgets import RUN
from tests.support.stop import decide, drive_stop, obedient_stop, records, view_of
from tests.support.tasks import AGENT, TaskLog, demo_tree, has, ref


def _log() -> TaskLog:
    log = TaskLog()
    log.started()
    return log


def _next() -> StopCandidate:
    return StopCandidate(RUN, "next")


def _finish(instance: InstanceRef, turn: int | None = 1) -> StopCandidate:
    return StopCandidate(RUN, "finish", instance, after_turn=turn)


def test_next_pushes_the_first_pick_and_gives_it_its_first_turn() -> None:
    log, tree = _log(), demo_tree()
    d = decide(log, tree, _next())
    assert d.verdict.kind == "continue"
    assert d.instance == ref("scope")
    assert d.trail == (("run:R-0001", "push"), ("task:scope", "continue"))
    assert d.stack == ("run:R-0001",)
    assert d.turn == 1
    assert [f.type for f in d.facts] == [
        "task.entered", "completion_check.evaluated", "stop.decided",
    ]  # fmt: skip
    assert d.record.payload["candidate"] == "next"
    assert d.record.instance == "scope"


def test_a_passing_finish_pops_and_the_run_pushes_the_next_instance_in_one_decision() -> None:
    log, tree = _log(), demo_tree()
    decide(log, tree, _next())
    log.select("A", "B")
    d = decide(log, tree, _finish(ref("scope")))
    assert d.pops == (("scope", "done"),)
    assert d.instance == ref("extract", "A")
    assert d.verdict.kind == "continue"
    assert d.record.payload["candidate_instance"] == "scope"
    view = log.view()
    assert statuses(view, tree, RUN)["scope"] == "done"
    assert vetoes_since_entry(view, RUN, "extract[A]") == 0  # a first turn, not a veto


def test_a_failing_finish_is_a_veto_with_the_check_message() -> None:
    log, tree = _log(), demo_tree()
    decide(log, tree, _next())
    d = decide(log, tree, _finish(ref("scope")))
    assert d.verdict.kind == "continue"
    assert d.verdict.reason == "save selection first"
    assert vetoes_since_entry(log.view(), RUN, "scope") == 1
    assert turns_used(log.view(), RUN, "scope") == 2


def test_the_retry_cap_fails_the_instance_at_the_third_refused_finish() -> None:
    log, tree = _log(), demo_tree()
    decide(log, tree, _next())
    decide(log, tree, _finish(ref("scope"), 1))
    decide(log, tree, _finish(ref("scope"), 2))
    d = decide(log, tree, _finish(ref("scope"), 3))
    assert d.replaced == ("task:scope", "continue")
    assert d.limit == "stop_veto_limit"
    assert d.counts["vetoes"] == 3
    assert d.pops == (("scope", "fail"),)
    assert statuses(log.view(), tree, RUN)["scope"] == "blocked"
    assert d.verdict.kind == "stop" and d.verdict.outcome == "partial"
    ended = [e for e in log.view().events if e.type == "run.ended"]
    assert ended[0].payload["outcome"] == "partial"
    assert ended[0].payload["blockers"] == ["scope"]


def test_the_turn_limit_fails_the_instance() -> None:
    tree = compile_tree(AGENT, (Task("only", writes=("x",), done=has("x"), turn_limit=2),))
    log = _log()
    decide(log, tree, _next())
    decide(log, tree, StopCandidate(RUN, "tools", ref("only"), after_turn=1))
    d = decide(log, tree, StopCandidate(RUN, "tools", ref("only"), after_turn=2))
    assert d.limit == "turn_limit"
    assert d.counts == {"turns": 2, "allowed": 2}
    assert d.pops == (("only", "fail"),)
    assert d.verdict.kind == "stop"


def test_a_spent_budget_raises_the_budget_interrupt_and_keeps_the_work() -> None:
    tree = demo_tree()
    log = _log()
    budget = Budget(model_turns=1)
    decide(log, tree, _next(), budget=budget)
    log.select("A")
    d = decide(log, tree, StopCandidate(RUN, "tools", ref("scope"), after_turn=1), budget=budget)
    assert d.verdict.kind == "hold"
    assert d.replaced == ("task:scope", "continue")
    assert d.limit == "model_turns"
    types = [f.type for f in d.facts]
    assert types == ["budget.reached", "interrupt.raised", "stop.decided"]
    assert d.record.payload["interrupt"] == "budget-1"
    assert d.instance is None  # the budget interrupt holds the run
    view = log.view()
    assert any(e.type == "artifact.saved" for e in view.events)  # partial work stays
    # A held run takes no turn until a person grants more.
    again = decide(log, tree, _next(), budget=budget)
    assert again.verdict.kind == "hold" and again.decided_by == "interrupt:budget-1"
    log.granted({"model_turns": 5}, interrupt_id="budget-1")
    log.resolve("budget-1")
    after = decide(log, tree, _next(), budget=budget)
    assert after.verdict.kind == "continue" and after.instance == ref("scope")


def test_a_pending_stop_request_wins_before_any_policy() -> None:
    log, tree = _log(), demo_tree()
    decide(log, tree, _next())
    log.add("run.stop_requested", {"scope": "run", "reason": "r", "by": "p", "command_id": "c"})
    d = decide(log, tree, _finish(ref("scope")))
    assert d.verdict.kind == "stop" and d.verdict.outcome == "stopped"
    assert d.trail == (("run:R-0001", "stop"),)
    assert [f.type for f in d.facts] == ["run.ended", "stop.decided"]
    assert d.facts[0].payload == {"outcome": "stopped", "reason": "stop_requested"}


def test_an_open_interrupt_holds_its_instance_only() -> None:
    log, tree = _log(), demo_tree()
    decide(log, tree, _next())
    log.select("A", "B")
    decide(log, tree, _finish(ref("scope")))  # pushes extract[A]
    log.raise_interrupt("extract[A]", "I-0001")
    held = decide(log, tree, StopCandidate(RUN, "interrupted", ref("extract", "A")))
    assert held.verdict.kind == "hold"
    assert held.decided_by == "interrupt:I-0001"
    assert held.instance == ref("extract", "A")
    other = decide(log, tree, _next())  # S1: the run's other work goes on
    assert other.verdict.kind == "continue"
    assert other.instance == ref("extract", "B")


def test_nothing_runnable_and_an_instance_waiting_holds_the_run() -> None:
    tree = compile_tree(AGENT, (Task("ask", writes=("x",), done=has("x")),))
    log = _log()
    decide(log, tree, _next())
    log.raise_interrupt("ask", "I-0001")
    decide(log, tree, StopCandidate(RUN, "interrupted", ref("ask")))
    d = decide(log, tree, _next())
    assert d.verdict.kind == "hold" and d.instance is None
    assert d.decided_by == "run:R-0001"
    assert "I-0001" in d.verdict.reason


def test_an_obedient_run_ends_complete_with_one_decision_per_candidate() -> None:
    log, tree = _log(), demo_tree()
    decisions = drive_stop(log, tree, obedient_stop(("A", "B")))
    assert decisions[-1].verdict.outcome == "complete"
    assert len(records(log)) == len(decisions)  # test_one_stop_decision_per_candidate
    assert [d.record.payload["walk"] for d in decisions] == list(range(1, len(decisions) + 1))


def test_only_continue_spends_a_turn() -> None:
    log, tree = _log(), demo_tree()
    decisions = drive_stop(log, tree, obedient_stop(("A", "B")))
    continues = sum(1 for d in decisions if d.verdict.kind == "continue")
    view = log.view()
    charged = sum(turns_used(view, RUN, i) for i in statuses(view, tree, RUN))
    assert charged == continues == 6  # scope, two extracts, two compares, report


def test_an_entry_whose_check_already_passes_takes_no_turn() -> None:
    log, tree = _log(), demo_tree()
    log.select("A")  # the selection exists before the run enters scope
    d = decide(log, tree, _next())
    assert d.pops == (("scope", "done"),)
    assert d.instance == ref("extract", "A")


def test_a_raising_check_is_fail_check_raised() -> None:
    def boom(view: Any) -> CheckResult:
        raise RuntimeError("bug")

    tree = compile_tree(AGENT, (Task("only", done=boom),))
    log = _log()
    first = decide(log, tree, _next())
    assert first.pops == ()  # entry: the raising check failed, so the instance runs
    d = decide(log, tree, _finish(ref("only")))
    assert d.pops == (("only", "fail"),)
    assert d.verdict.kind == "stop" and d.verdict.outcome == "partial"


def test_evaluation_is_a_pure_function_of_the_log() -> None:
    log, tree = _log(), demo_tree()
    decide(log, tree, _next())
    view = view_of(log, tree)
    c = _finish(ref("scope"))
    assert evaluate_stop(view, c) == evaluate_stop(view, c)
    assert len(log.events) == len(view.log.events)  # nothing was written


def test_an_ended_run_is_refused() -> None:
    log, tree = _log(), demo_tree()
    log.add("run.ended", {"outcome": "failed", "reason": "x"})
    with pytest.raises(StopRefused) as err:
        evaluate_stop(view_of(log, tree), _next())
    assert err.value.code == "run_not_open"


def test_a_run_fault_stops_the_run_failed() -> None:
    log, tree = _log(), demo_tree()
    d = decide(log, tree, StopCandidate(RUN, "fault", reason="definition_changed"))
    assert d.verdict.outcome == "failed"
    ended = [e for e in log.view().events if e.type == "run.ended"]
    assert ended[0].payload == {"outcome": "failed", "reason": "definition_changed"}


def test_a_candidate_kind_outside_the_set_is_refused() -> None:
    with pytest.raises(ValueError):
        StopCandidate(RUN, "paused")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Verdict("push")
    with pytest.raises(ValueError):
        Verdict("stop")


def test_a_parent_entry_passes_up_and_its_children_run_then_it_pops_once_they_settle() -> None:
    child = Task("edit", writes=("x",), done=has("x"))
    tree = compile_tree(AGENT, (Task("work", children=(child,)),))
    log = _log()
    first = decide(log, tree, _next())
    assert first.trail == (
        ("run:R-0001", "push"), ("task:work", "pass"),
        ("run:R-0001", "push"), ("task:work/edit", "continue"),
    )  # fmt: skip
    assert first.instance == ref("work/edit")
    log.save("x", None, {"v": 1})
    last = decide(log, tree, _finish(ref("work/edit")))
    assert last.pops == (("work/edit", "done"), ("work", "done"))
    assert last.verdict.kind == "stop" and last.verdict.outcome == "complete"
    assert statuses(log.view(), tree, RUN) == {"work": "done", "work/edit": "done"}


def test_a_supervisor_pushes_its_runnable_child_and_finishes_when_its_check_passes() -> None:
    child = Task("edit", writes=("x",), done=has("x"))
    boss = Task("lead", children=(child,), brief="Steer the work.", done=has("x"))
    tree = compile_tree(AGENT, (boss,))
    log = _log()
    first = decide(log, tree, _next())
    assert first.instance == ref("lead/edit") and first.verdict.kind == "continue"
    log.save("x", None, {"v": 1})
    last = decide(log, tree, _finish(ref("lead/edit")))
    assert last.pops == (("lead/edit", "done"), ("lead", "done"))
    assert last.verdict.outcome == "complete"
