"""Stop-policy evaluation against a real SQLite event store (ADR-P16 D3, D7).

The record's dedup key makes a restart that reaches the same decision write nothing; a
kill inside the commit leaves one record with the same verdict; a kill while a person
holds the run writes one HOLD however many restarts follow.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.stop_policy.commit import commit_stop_decision
from aksha.runtime.stop_policy.evaluation import awaiting_person, evaluate_stop
from aksha.runtime.stop_policy.hold import held_auto_starts
from aksha.runtime.stop_policy.types import StopCandidate, StopView
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import Task
from tests.support.budgets import RUN
from tests.support.stop import records
from tests.support.tasks import AGENT, StoreLedger, demo_tree, has, ref


class Killed(BaseException):
    """A process kill: not an ``Exception``, so nothing in the runtime catches it."""


def _ledger(tmp_path: Path) -> StoreLedger:
    ledger = StoreLedger(tmp_path / "aksha.sqlite")
    started = NewEvent("run.started", {"agent": "a", "cause": "request"}, run=RUN)
    ledger.commit([started], boundary="U1")
    return ledger


def _view(ledger: StoreLedger, tree: Any) -> StopView:
    return StopView(ledger.view(), tree, RUN)


def test_a_restart_that_reaches_the_same_decision_writes_nothing(tmp_path: Path) -> None:
    ledger, tree = _ledger(tmp_path), demo_tree()
    before = _view(ledger, tree)
    d = evaluate_stop(before, StopCandidate(RUN, "next"))
    assert commit_stop_decision(ledger.store, ledger.owner, d) is True
    assert commit_stop_decision(ledger.store, ledger.owner, d) is False
    ledger = ledger.reopen()
    again = evaluate_stop(StopView(ledger.view().upto(before.log.seq_read), tree, RUN), d.candidate)
    assert again == d
    assert commit_stop_decision(ledger.store, ledger.owner, again) is False
    assert len(records(ledger)) == 1


def test_a_kill_inside_the_commit_leaves_one_record_with_the_same_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fault-injection point 12: the transaction is all or nothing."""
    ledger, tree = _ledger(tmp_path), demo_tree()
    c = StopCandidate(RUN, "next")
    d = evaluate_stop(_view(ledger, tree), c)
    real = ledger.store.append

    def killed(owner: str, boundary: str, events: Sequence[NewEvent], *a: Any, **k: Any) -> Any:
        raise Killed

    monkeypatch.setattr(ledger.store, "append", killed)
    with pytest.raises(Killed):
        commit_stop_decision(ledger.store, ledger.owner, d)
    monkeypatch.setattr(ledger.store, "append", real)
    ledger = ledger.reopen()
    assert records(ledger) == []
    after = evaluate_stop(_view(ledger, tree), c)
    assert after.verdict == d.verdict and after.trail == d.trail
    assert commit_stop_decision(ledger.store, ledger.owner, after) is True
    assert len(records(ledger)) == 1


def test_a_kill_while_a_person_holds_the_run_writes_one_hold(tmp_path: Path) -> None:
    tree = compile_tree(AGENT, (Task("ask", writes=("x",), done=has("x")),))
    ledger = _ledger(tmp_path)
    first = evaluate_stop(_view(ledger, tree), StopCandidate(RUN, "next"))
    commit_stop_decision(ledger.store, ledger.owner, first)
    ledger.raise_interrupt("ask", "A-0001")  # the turn asked for an approval (U2)
    ledger = ledger.reopen()  # killed before the stop decision
    held = evaluate_stop(_view(ledger, tree), StopCandidate(RUN, "approval", ref("ask")))
    assert held.verdict.kind == "hold" and held.instance == ref("ask")
    commit_stop_decision(ledger.store, ledger.owner, held)
    run_hold = evaluate_stop(_view(ledger, tree), StopCandidate(RUN, "next"))
    assert run_hold.verdict.kind == "hold" and run_hold.instance is None
    commit_stop_decision(ledger.store, ledger.owner, run_hold)
    for _ in range(2):  # two more kills: each restart finds the run waiting, writes nothing
        ledger = ledger.reopen()
        assert awaiting_person(ledger.view(), RUN) is True
    assert len(records(ledger)) == 3
    ledger.resolve("A-0001")
    assert awaiting_person(ledger.view(), RUN) is False
    resumed = evaluate_stop(_view(ledger, tree), StopCandidate(RUN, "next"))
    assert resumed.verdict.kind == "continue" and resumed.instance == ref("ask")
    commit_stop_decision(ledger.store, ledger.owner, resumed)
    assert held_auto_starts(ledger.view()) == ()
