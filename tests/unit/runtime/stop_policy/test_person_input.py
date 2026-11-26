"""A person's pending input at a finish (ADR-P27 D4): a CONTINUE with their words, no veto."""

from __future__ import annotations

from aksha.runtime.budgets.limits import vetoes_since_entry
from aksha.runtime.stop_policy.types import StopCandidate
from tests.support.budgets import RUN
from tests.support.stop import decide
from tests.support.tasks import TaskLog, demo_tree, ref


def _queued(log: TaskLog, ident: str, lane: str, text: str, instance: str | None = None) -> None:
    log.add("input.queued", {"input_id": ident, "lane": lane, "text": text,
                             "instance": instance, "by": "person",
                             "command_id": f"c-{ident}"})  # fmt: skip


def _started() -> TaskLog:
    log = TaskLog()
    log.started()
    decide(log, demo_tree(), StopCandidate(RUN, "next"))
    return log


def test_a_pending_follow_up_turns_a_finish_into_continue_and_is_consumed() -> None:
    log, tree = _started(), demo_tree()
    _queued(log, "IN-0001", "follow_up", "add a summary")
    d = decide(log, tree, StopCandidate(RUN, "finish", ref("scope"), after_turn=1))
    assert d.verdict.kind == "continue" and d.verdict.code == "person_input"
    assert "add a summary" in d.verdict.reason
    assert d.record.payload["inputs"] == ["IN-0001"]
    (consumed,) = [f for f in d.facts if f.type == "input.consumed"]
    assert consumed.payload == {"input_id": "IN-0001", "by": "stop_policy",
                                "step_id": "R-0001/scope/t02"}  # fmt: skip
    assert not any(f.type == "completion_check.evaluated" for f in d.facts)
    assert vetoes_since_entry(log.view(), RUN, "scope") == 0
    again = decide(log, tree, StopCandidate(RUN, "finish", ref("scope"), after_turn=2))
    assert again.verdict.reason == "save selection first"  # the check judges the next finish


def test_a_follow_up_for_another_instance_waits_for_that_one() -> None:
    log, tree = _started(), demo_tree()
    _queued(log, "IN-0001", "follow_up", "later", instance="extract[A]")
    d = decide(log, tree, StopCandidate(RUN, "finish", ref("scope"), after_turn=1))
    assert d.verdict.code != "person_input"
    assert not any(f.type == "input.consumed" for f in d.facts)


def test_a_steer_pending_at_a_finish_is_read_by_the_next_turn_not_dropped() -> None:
    log, tree = _started(), demo_tree()
    _queued(log, "IN-0001", "steer", "use the helper")
    d = decide(log, tree, StopCandidate(RUN, "finish", ref("scope"), after_turn=1))
    assert d.verdict.kind == "continue" and d.verdict.code == "person_input"
    assert d.record.payload["inputs"] == ["IN-0001"]
    assert not any(f.type == "input.consumed" for f in d.facts)  # the context consumes it


def test_a_follow_up_for_a_supervisor_vetoes_its_finish() -> None:
    """B4 for a parent that takes turns: its finish waits for the person's follow-up."""
    from aksha.runtime.tasks.tree import compile_tree
    from aksha.runtime.tasks.types import Task
    from tests.support.tasks import AGENT, has

    child = Task("edit", writes=("x",), done=has("x"))
    boss = Task("lead", children=(child,), brief="Steer the work.", done=has("x"))
    tree = compile_tree(AGENT, (boss,))
    log = TaskLog()
    log.started()
    decide(log, tree, StopCandidate(RUN, "next"))
    _queued(log, "IN-0001", "follow_up", "add a summary", instance="lead")
    log.save("x", None, {"v": 1})
    d = decide(log, tree, StopCandidate(RUN, "finish", ref("lead/edit"), after_turn=1))
    assert d.verdict.kind == "continue" and d.verdict.code == "person_input"
    assert d.instance == ref("lead") and "add a summary" in d.verdict.reason
    (consumed,) = [f for f in d.facts if f.type == "input.consumed"]
    assert consumed.payload["step_id"].startswith("R-0001/lead/")
