"""Finalization and the reserved budget (ADR-P24 D4, D5), on an in-memory log."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from aksha.runtime.budgets.meter import Meter
from aksha.runtime.budgets.types import Dimension
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.finalization.finalize import finalize_facts, to_void
from aksha.runtime.finalization.reserve import finalizer_paths, held_back, reserve_for
from aksha.runtime.finalization.state import finalizing
from aksha.runtime.finalization.types import UnknownEntry
from aksha.runtime.finalization.unknowns import unknowns
from aksha.runtime.grounding.types import Unknown
from aksha.runtime.tasks.scheduler import run_outcome, runnable
from aksha.runtime.tasks.view import TaskView
from tests.support.budgets import RUN
from tests.support.partial import VENDORS, research_tree, research_turn
from tests.support.tasks import TaskLog, demo_tree, drive, ref

USED: Mapping[Dimension, int] = MappingProxyType(
    {"model_turns": 9, "tool_calls": 36, "active_ms": 0, "tokens": 0, "cost_micro_usd": 0}
)
ALLOWED: Mapping[Dimension, int] = MappingProxyType(
    {"model_turns": 25, "tool_calls": 40, "active_ms": 120_000}
)


def _budget_spent(log: TaskLog) -> None:
    """Scope and two vendors done, a third entered, then the budget interrupt."""
    log.pop("scope")
    log.select(*VENDORS)
    log.pop("fetch_extract[COMP-01]")
    log.save("record", "COMP-01", {"vendor": "COMP-01", "price": 100})
    log.pop("fetch_extract[COMP-02]")
    log.save("record", "COMP-02", {"vendor": "COMP-02", "price": 100})
    log.add("task.entered", {"cause": "scheduler", "task": "fetch_extract", "key": "COMP-03"},
            instance="fetch_extract[COMP-03]")  # fmt: skip
    log.add("interrupt.raised", {"interrupt": "budget-1", "kind": "budget"})


def _finalize(log: TaskLog, tree: object) -> None:
    facts = finalize_facts(log.view(), tree, RUN, "budget-1", "person")  # type: ignore[arg-type]
    log.commit(facts, boundary="U4")
    log.commit(
        [NewEvent("interrupt.resolved", {"interrupt": "budget-1", "decision": "finalized"},
                  run=RUN)],
        boundary="U4",
    )  # fmt: skip


def test_reserve_is_a_share_of_each_capped_dimension() -> None:
    tree = research_tree()
    assert finalizer_paths(tree) == ("matrix",)
    assert dict(reserve_for(tree, ALLOWED)) == {
        "model_turns": 2, "tool_calls": 4, "active_ms": 12_000,
    }  # fmt: skip
    assert dict(reserve_for(tree, {"model_turns": 2, "tool_calls": 1})) == {"model_turns": 1}
    assert dict(reserve_for(demo_tree(), ALLOWED)) == {}


def test_non_finalizers_never_draw_on_the_reserve() -> None:
    """D5: before finalizing, the allowance is lowered by the reserve; after, it is whole."""
    tree, log = research_tree(), TaskLog()
    m = Meter(USED, ALLOWED)
    assert m.exhausted() == ()
    held = held_back(log.view(), tree, RUN, m)
    assert held.allowed["tool_calls"] == 36 and held.exhausted() == ("tool_calls",)
    assert held_back(log.view(), demo_tree(), RUN, m) is m
    _budget_spent(log)
    _finalize(log, tree)
    assert held_back(log.view(), tree, RUN, m) is m


def test_finalize_after_budget() -> None:
    """D4: unstarted keys become ``not_checked`` and only the finalizer runs."""
    tree, log = research_tree(), TaskLog()
    _budget_spent(log)
    assert to_void(log.view(), tree, RUN) == (
        "fetch_extract[COMP-03]", "fetch_extract[COMP-04]", "fetch_extract[COMP-05]",
    )  # fmt: skip
    _finalize(log, tree)
    found = finalizing(log.view(), RUN)
    assert found is not None
    assert (found.interrupt, found.finalizers) == ("budget-1", ("matrix",))
    assert dict(found.reserve)["model_turns"] == 2
    assert [r.instance for r in runnable(log.view(), tree, RUN)] == ["matrix"]
    entered = drive(log, tree, research_turn(tree))
    assert entered == ["matrix"]
    matrix = TaskView(log.view(), tree, ref("matrix")).latest("matrix")
    assert matrix is not None
    rows = matrix.content["rows"]
    assert [k for k, v in rows.items() if "vendor" in v] == ["COMP-01", "COMP-02"]
    assert rows["COMP-05"] == {"unknown": {"question": "demo.record for COMP-05",
                                           "reason": "not_checked"}}  # fmt: skip
    gaps = [UnknownEntry(f"fetch_extract[COMP-0{n}]", "not_checked", "budget-1") for n in (3, 4, 5)]
    assert unknowns(log.view(), tree, RUN) == tuple(gaps)
    outcome = run_outcome(log.view(), tree, RUN)
    assert outcome is not None
    assert (outcome.outcome, outcome.unknowns) == ("partial", tuple(gaps))


def test_a_finalized_run_is_partial_even_with_every_finalizer_done() -> None:
    """D6: finalization always leaves the run ``partial``, never ``complete``."""
    tree, log = research_tree(), TaskLog()
    _budget_spent(log)
    _finalize(log, tree)
    drive(log, tree, research_turn(tree))
    outcome = run_outcome(log.view(), tree, RUN)
    assert outcome is not None and outcome.outcome == "partial"
    entry = TaskView(log.view(), tree, ref("matrix")).entries("record")["COMP-03"]
    assert entry == Unknown("demo.record for COMP-03", "not_checked")


def test_finalizing_voids_the_open_interrupts_of_voided_work() -> None:
    tree, log = research_tree(), TaskLog()
    _budget_spent(log)
    log.add("interrupt.raised", {"interrupt": "I-0001", "kind": "input"},
            instance="fetch_extract[COMP-03]")  # fmt: skip
    facts = finalize_facts(log.view(), tree, RUN, "budget-1", "person")
    voids = [f.payload for f in facts if f.type == "interrupt.voided"]
    assert voids == [{"interrupt": "I-0001", "reason": "finalizing"}]


def test_a_tree_without_finalizers_finalizes_to_what_it_saved() -> None:
    """No finalizer: nothing runs after ``finalize`` and the run ends ``partial``."""
    tree, log = research_tree(finalizer=False), TaskLog()
    _budget_spent(log)
    _finalize(log, tree)
    assert runnable(log.view(), tree, RUN) == ()
    outcome = run_outcome(log.view(), tree, RUN)
    assert outcome is not None and outcome.outcome == "partial"
    assert [u.key for u in outcome.unknowns] == [
        "fetch_extract[COMP-03]", "fetch_extract[COMP-04]", "fetch_extract[COMP-05]", "matrix",
    ]  # fmt: skip
