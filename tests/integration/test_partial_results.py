"""Partial results and finalization end to end (ADR-P24 Tests), on SQLite.

The research tree runs on a real event store, so the catalogue's schemas and boundaries
accept every fact: the worked example (one vendor blocked), the ``finalize`` command and
its refusals, and the hold predicate on a finalizer's start (D7). Then a small agent runs
through the agent loop itself: its worker spends the budget short of the reserve, the
person answers the budget interrupt with ``finalize``, the finalizer runs on the reserve,
and ``run.ended`` lists the unknowns (D5, D6).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.cancellation.command import stop
from aksha.runtime.definitions.types import Agent
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.finalization.state import finalizing
from aksha.runtime.finalization.types import UnknownEntry
from aksha.runtime.interrupts.commands import ALREADY_FINALIZING, finalize, grant_budget
from aksha.runtime.interrupts.raising import raised
from aksha.runtime.interrupts.reducers import open_interrupts
from aksha.runtime.interrupts.types import InterruptRequest, Question
from aksha.runtime.models.types import GenerationRequest, GenerationResult
from aksha.runtime.stop_policy import hold as holds
from aksha.runtime.tasks.scheduler import run_outcome, runnable
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import CheckResult, Task
from aksha.runtime.tasks.view import TaskView
from tests.support.budgets import RUN
from tests.support.loop import AGENT, NOTE, OWNER, Bench, Recorder, call, tools_reply
from tests.support.partial import VENDORS, research_tree, research_turn
from tests.support.tasks import StoreLedger, drive, ref

COMP_04 = "fetch_extract[COMP-04]"
ASK = (Question("q1", "Which vendors?"),)


def _raise_budget(ledger: StoreLedger) -> None:
    request = InterruptRequest("budget", run=RUN, extra={"limit": "tool_calls"})
    ledger.commit([raised(request, "budget-1")], boundary="U3")


def test_one_vendor_blocked_run_partial_through_the_store(tmp_path: Path) -> None:
    ledger = StoreLedger(tmp_path / "aksha.sqlite")
    tree = research_tree()
    drive(ledger, tree, research_turn(tree))
    reopened = ledger.reopen()
    outcome = run_outcome(reopened.view(), tree, RUN)
    assert outcome is not None and outcome.outcome == "partial"
    assert outcome.unknowns == (UnknownEntry(COMP_04, "unverified", COMP_04),)
    rows = TaskView(reopened.view(), tree, ref("matrix")).latest("matrix")
    assert rows is not None and rows.content["rows"]["COMP-04"]["unknown"]["reason"] == (
        "unverified"
    )
    assert reopened.store.verify() is None


class _BudgetSpent(Exception):
    """The scripted model stops here: the budget interrupt is raised and the run holds."""


def _spend_until_budget(ledger: StoreLedger) -> Any:
    """Scope and two vendors done; the third's turn raises the budget interrupt."""
    tree = research_tree()
    turn = research_turn(tree)

    def until_budget(pick: Any, current: Any) -> str:
        if pick.key == "COMP-03":
            _raise_budget(current)
            raise _BudgetSpent
        return str(turn(pick, current))

    with pytest.raises(_BudgetSpent):
        drive(ledger, tree, until_budget)
    return tree


def test_finalize_command_answers_only_an_open_budget_interrupt(tmp_path: Path) -> None:
    ledger = StoreLedger(tmp_path / "aksha.sqlite")
    tree = _spend_until_budget(ledger)
    assert [i.id for i in open_interrupts(ledger.view(), RUN)] == ["budget-1"]
    ask = InterruptRequest("input", run=RUN, instance="matrix", questions=ASK)
    ledger.commit([raised(ask, "I-0001")], boundary="U2")
    wrong = finalize(ledger.store, ledger.owner, "I-0001", tree=tree, by="person")
    assert wrong.refused == "not_a_budget_interrupt"
    ok = finalize(ledger.store, ledger.owner, "budget-1", tree=tree, by="person",
                  command_id="c-final")  # fmt: skip
    assert ok.refused is None
    again = finalize(ledger.store, ledger.owner, "budget-1", tree=tree, by="person",
                     command_id="c-final")  # fmt: skip
    assert again.duplicate and again.seq == ok.seq  # a retry returns the first receipt
    found = finalizing(ledger.view(), RUN)
    assert found is not None and found.voided == (
        "fetch_extract[COMP-03]", "fetch_extract[COMP-04]", "fetch_extract[COMP-05]",
    )  # fmt: skip
    _raise_budget_two(ledger)
    late = finalize(ledger.store, ledger.owner, "budget-2", tree=tree, by="person")
    assert late.refused == ALREADY_FINALIZING
    granted = grant_budget(ledger.store, ledger.owner, "budget-1", {"tool_calls": 1}, by="p")
    assert granted.refused == "already_decided"


def _raise_budget_two(ledger: StoreLedger) -> None:
    request = InterruptRequest("budget", run=RUN, extra={"limit": "tool_calls"})
    ledger.commit([raised(request, "budget-2")], boundary="U3")


def test_finalize_after_budget_through_the_store(tmp_path: Path) -> None:
    ledger = StoreLedger(tmp_path / "aksha.sqlite")
    tree = _spend_until_budget(ledger)
    finalize(ledger.store, ledger.owner, "budget-1", tree=tree, by="person")
    assert [r.instance for r in runnable(ledger.view(), tree, RUN)] == ["matrix"]
    entered = drive(ledger, tree, research_turn(tree))
    assert entered == ["matrix"]
    outcome = run_outcome(ledger.view(), tree, RUN)
    assert outcome is not None and outcome.outcome == "partial"
    assert [u.reason for u in outcome.unknowns] == ["not_checked"] * 3
    assert len(VENDORS) - len(outcome.unknowns) == 2


def test_a_stop_request_still_wins_over_finalization(tmp_path: Path) -> None:
    """D7: the finalizer's start calls the hold predicate; a person's stop holds it."""
    ledger = StoreLedger(tmp_path / "aksha.sqlite")
    started = {"agent": "demo", "cause": "request"}
    ledger.commit([NewEvent("run.started", started, run=RUN)], boundary="U1")
    tree = _spend_until_budget(ledger)
    finalize(ledger.store, ledger.owner, "budget-1", tree=tree, by="person")
    (pick,) = runnable(ledger.view(), tree, RUN)
    assert holds.hold_reason(ledger.view(), pick) is None
    assert stop(ledger.store, ledger.owner, RUN, by="person").refused is None
    held = holds.hold_reason(ledger.view(), pick)
    assert held is not None and held.reason == "stop_requested"


# --- through the agent loop -----------------------------------------------------------


def _has_note(view: TaskView) -> CheckResult:
    return CheckResult(view.latest("note") is not None, "save a note first")


def _never(view: TaskView) -> CheckResult:
    return CheckResult(False, "keep testing")


def _agent(base: Agent) -> Agent:
    """``coder`` with a worker that never finishes and a ``summary`` finalizer."""
    tasks = (
        Task("edit", tools=("run_tests",), done=_never, turn_limit=250),
        Task("summary", tools=("run_tests", "save_artifact"), writes=("note",),
             needs=("edit",), done=_has_note, finalizer=True),
    )  # fmt: skip
    return replace(base, tasks=tasks)  # ``coder`` already lists ``save_artifact``


def _rule(summary_calls: int) -> Any:
    """``edit`` runs the tests every turn; ``summary`` runs them, then saves and finishes."""
    seen = {"summary": 0}

    def rule(request: GenerationRequest) -> GenerationResult:
        if "/summary/" not in request.step_id:
            return tools_reply(call("run_tests"))
        seen["summary"] += 1
        if seen["summary"] <= summary_calls:
            return tools_reply(call("run_tests", seen["summary"]))
        save = call("save_artifact", kind="note", content={"summary": "partial"})
        return tools_reply(save, call("finish", summary="done"))

    return rule


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Any]:
    benches: list[Bench] = []

    def build(summary_calls: int) -> Bench:
        budget = {"model_turns": 1_000, "tool_calls": 20, "active_ms": 10**9}
        b = Bench(tmp_path / f"aksha-{len(benches)}.sqlite", Recorder(rule=_rule(summary_calls)),
                  budget=budget)  # fmt: skip
        b.agent = _agent(b.agent)
        b.setup = replace(
            b._setup(b.store),
            tree=compile_tree(AGENT, b.agent.tasks),
            artifacts=ArtifactStore({f"{AGENT}.note": NOTE}),
        )
        b.start()
        benches.append(b)
        return b

    yield build
    for b in benches:
        b.close()


def test_finalize_runs_the_finalizer_on_the_reserve(make: Any) -> None:
    b = make(summary_calls=0)
    held = b.drive()
    assert held.state == "held"
    # The reserve (10% of 20 tool calls) is held back: edit stops at 18 of 20.
    assert b.bodies.calls == ["run_tests"] * 18
    assert [e.payload["kind"] for e in b.events("interrupt.raised")] == ["budget"]
    receipt = finalize(b.store, OWNER, "budget-1", tree=b.setup.tree, by="person")
    assert receipt.refused is None
    ended = b.drive()
    assert (ended.state, ended.outcome) == ("ended", "partial")
    payload = b.ended()
    assert payload["outcome"] == "partial"
    assert [dict(u) for u in payload["unknowns"]] == [
        {"key": "edit", "reason": "not_checked", "blocker": "budget-1"}
    ]
    assert b.bodies.calls == ["run_tests"] * 18  # the finalizer saved and finished


def test_spending_the_reserve_too_ends_the_run_partial(make: Any) -> None:
    """D5: a finalizer that spends the reserve ends the run ``partial`` with what it saved."""
    b = make(summary_calls=10)
    b.drive()
    finalize(b.store, OWNER, "budget-1", tree=b.setup.tree, by="person")
    ended = b.drive()
    assert (ended.state, ended.outcome) == ("ended", "partial")
    payload = b.ended()
    assert payload["reason"].startswith("reserve spent: tool_calls")
    assert [dict(u) for u in payload["unknowns"]] == [
        {"key": "edit", "reason": "not_checked", "blocker": "budget-1"},
        {"key": "summary", "reason": "not_checked", "blocker": "reserve_spent"},
    ]
    assert len(b.events("interrupt.raised")) == 1  # no second budget interrupt
