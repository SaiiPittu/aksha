"""The hold predicate (ADR-P16 D6, S1) and its audit (D7), with the mutation that proves it."""

from __future__ import annotations

import pytest

from aksha.runtime.artifacts.types import ArtifactRef, WriterId
from aksha.runtime.invalidation.types import RebuildEntry
from aksha.runtime.stop_policy import hold as holds
from aksha.runtime.stop_policy.hold import held_auto_starts, hold_reason, rebuild_blocked
from aksha.runtime.stop_policy.types import (
    AgentScope,
    Hold,
    RunScope,
    SessionScope,
    StopCandidate,
)
from aksha.runtime.tasks.types import InstanceRef
from tests.support.budgets import RUN
from tests.support.stop import decide, drive_stop, obedient_stop
from tests.support.tasks import TaskLog, demo_tree, ref


def _log() -> TaskLog:
    log = TaskLog()
    log.started()
    return log


def _stop_request(log: TaskLog) -> int:
    payload = {"scope": "run", "reason": "r", "by": "p", "command_id": "c-1"}
    return log.add("run.stop_requested", payload).seq


def _entry(task_path: str, key: str | None, agent: str = "a") -> RebuildEntry:
    slot = f"demo.item/{key}@v1" if key else "demo.item@v1"
    return RebuildEntry(WriterId(agent, task_path, key), "regenerate", ArtifactRef.parse(slot), ())


def test_an_instance_is_held_by_its_own_open_interrupt() -> None:
    log = _log()
    seq = log.raise_interrupt("extract[A]", "I-0001").seq
    view = log.view()
    held = Hold("interrupt_open", seq, "R-0001/extract[A]")
    assert hold_reason(view, ref("extract", "A")) == held
    assert hold_reason(view, ref("extract", "B")) is None
    log.resolve("I-0001")
    assert hold_reason(log.view(), ref("extract", "A")) is None


def test_a_route_question_holds_only_its_session() -> None:
    """ADR-P17 S5: an open ``route`` holds the session's intake; no run or agent is held."""
    log = _log()
    raised = {"interrupt": "I-0009", "kind": "route", "options": ["a", "b"]}
    seq = log.add("interrupt.raised", raised, run=None, session="S-1").seq
    view = log.view()
    assert hold_reason(view, SessionScope("S-1")) == Hold("interrupt_open", seq, "S-1")
    assert hold_reason(view, SessionScope("S-2")) is None
    assert hold_reason(view, RunScope(RUN)) is None
    assert hold_reason(view, AgentScope("a")) is None
    log.add("interrupt.resolved", {"interrupt": "I-0009", "by": "p"}, run=None, session="S-1")
    assert hold_reason(log.view(), SessionScope("S-1")) is None


def test_s1_one_instance_interrupt_holds_neither_the_run_nor_the_agent() -> None:
    log = _log()
    log.raise_interrupt("extract[A]", "I-0001")
    view = log.view()
    assert hold_reason(view, RunScope(RUN)) is None
    assert hold_reason(view, AgentScope("a")) is None


def test_a_run_scoped_interrupt_holds_the_run_and_every_instance_of_it() -> None:
    log = _log()
    seq = log.add("interrupt.raised", {"interrupt": "budget-1", "kind": "budget"}).seq
    view = log.view()
    assert hold_reason(view, RunScope(RUN)) == Hold("interrupt_open", seq, RUN)
    assert hold_reason(view, ref("scope")) == Hold("interrupt_open", seq, RUN)


def test_a_stop_request_holds_the_run_first() -> None:
    log = _log()
    log.raise_interrupt("extract[A]", "I-0001")
    seq = _stop_request(log)
    view = log.view()
    assert hold_reason(view, ref("extract", "A")) == Hold("stop_requested", seq, RUN)
    assert hold_reason(view, RunScope(RUN)) == Hold("stop_requested", seq, RUN)


def test_a_person_stop_holds_the_agent_until_their_next_command() -> None:
    log, tree = _log(), demo_tree()
    decide(log, tree, StopCandidate(RUN, "next"))
    seq = _stop_request(log)
    decide(log, tree, StopCandidate(RUN, "finish", ref("scope")))
    held = hold_reason(log.view(), AgentScope("a"))
    assert held == Hold("stopped_by_person", seq, "a")
    log.started(run="R-0002")  # a person asks for a new run of the agent
    assert hold_reason(log.view(), AgentScope("a")) is None


def test_rebuild_blocked_holds_one_entry_not_the_agent() -> None:
    log = _log()
    seq = log.raise_interrupt("extract[A]", "I-0001").seq
    view = log.view()
    assert rebuild_blocked(view, _entry("extract", "A")) == Hold(
        "interrupt_open", seq, "R-0001/extract[A]"
    )
    assert rebuild_blocked(view, _entry("extract", "B")) is None  # its sibling rebuilds


def test_rebuild_blocked_holds_every_entry_of_a_person_stopped_agent() -> None:
    log, tree = _log(), demo_tree()
    decide(log, tree, StopCandidate(RUN, "next"))
    _stop_request(log)
    decide(log, tree, StopCandidate(RUN, "finish", ref("scope")))
    held = rebuild_blocked(log.view(), _entry("extract", "B"))
    assert held is not None and held.reason == "stopped_by_person"


def _interrupted_run() -> TaskLog:
    """A run where extract[A] asks a question, extract[B] goes on, then A is answered."""
    log, tree = _log(), demo_tree()

    def turn(pick: InstanceRef, ledger: TaskLog) -> str:
        if pick == ref("extract", "A") and not any(
            e.type == "interrupt.raised" for e in ledger.events
        ):
            ledger.raise_interrupt(pick.instance, "I-0001")
            return "interrupted"
        return obedient_stop(("A", "B"))(pick, ledger)

    def person(ledger: TaskLog) -> bool:
        if any(e.type == "interrupt.resolved" for e in ledger.events):
            return False
        ledger.resolve("I-0001")
        return True

    decisions = drive_stop(log, tree, turn, answer=person)
    assert decisions[-1].verdict.outcome == "complete"
    return log


def test_the_audit_holds_over_a_run_with_an_interrupt() -> None:
    assert held_auto_starts(_interrupted_run().view()) == ()


def test_the_audit_holds_over_an_obedient_run() -> None:
    log = _log()
    drive_stop(log, demo_tree(), obedient_stop(("A", "B")))
    assert held_auto_starts(log.view()) == ()


def test_the_audit_catches_a_continue_of_a_held_instance() -> None:
    log = _log()
    log.raise_interrupt("scope", "I-0001")
    log.decided("continue", instance="scope", cause="continuation", turn=1)
    found = held_auto_starts(log.view())
    assert [(h.type, h.reason) for h in found] == [("stop.decided", "interrupt_open")]


def test_the_audit_lets_a_person_command_continue() -> None:
    log = _log()
    log.raise_interrupt("scope", "I-0001")
    log.decided("continue", instance="scope", cause="command:send", turn=1)
    assert held_auto_starts(log.view()) == ()


def test_hold_guard_mutation_fails_the_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """D7: with the predicate gone from evaluation, a held instance is continued and caught."""
    monkeypatch.setattr(holds, "hold_reason", lambda view, scope: None)
    log, tree = _log(), demo_tree()
    decide(log, tree, StopCandidate(RUN, "next"))
    log.raise_interrupt("scope", "I-0001")
    d = decide(log, tree, StopCandidate(RUN, "interrupted", ref("scope")))
    assert d.verdict.kind == "continue"  # nothing held it
    assert [h.reason for h in held_auto_starts(log.view())] == ["interrupt_open"]
