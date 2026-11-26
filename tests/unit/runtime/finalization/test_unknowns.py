"""Explicit unknowns and the run outcome (ADR-P24 D1 to D3, D6), on an in-memory log."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from aksha.runtime.context.reads import render_reads
from aksha.runtime.context.types import ContextPolicy
from aksha.runtime.finalization.types import UnknownEntry
from aksha.runtime.finalization.unknowns import unknowns
from aksha.runtime.grounding.types import Unknown
from aksha.runtime.tasks.scheduler import run_outcome
from aksha.runtime.tasks.status import statuses
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import InstanceRef
from aksha.runtime.tasks.view import TaskView
from tests.support.budgets import RUN
from tests.support.partial import research_tree, research_turn, strict_check
from tests.support.tasks import AGENT, TaskLog, demo_tree, drive, obedient, ref

COMP_04 = "fetch_extract[COMP-04]"


def test_one_vendor_blocked_run_partial() -> None:
    """The worked example: four grounded rows, one explicit unknown, outcome ``partial``."""
    tree, log = research_tree(), TaskLog()
    entered = drive(log, tree, research_turn(research_tree()))
    assert "matrix" in entered
    view = log.view()
    assert statuses(view, tree, RUN)[COMP_04] == "blocked"
    assert statuses(view, tree, RUN)["matrix"] == "done"
    matrix = TaskView(view, tree, ref("matrix")).latest("matrix")
    assert matrix is not None
    rows = matrix.content["rows"]
    assert sorted(k for k, v in rows.items() if "vendor" in v) == [
        "COMP-01", "COMP-02", "COMP-03", "COMP-05",
    ]  # fmt: skip
    assert rows["COMP-04"] == {
        "unknown": {
            "question": "demo.record for COMP-04",
            "reason": "unverified",
            "failed_step": COMP_04,
        }
    }
    outcome = run_outcome(view, tree, RUN)
    assert outcome is not None
    assert outcome.outcome == "partial"
    assert outcome.blockers == (COMP_04,)
    assert outcome.unknowns == (UnknownEntry(COMP_04, "unverified", COMP_04),)


def test_the_unknown_comes_from_the_read_view_not_an_artifact() -> None:
    """D2: the blocked key reads as an ``Unknown``; no artifact is written for it."""
    tree, log = research_tree(), TaskLog()
    drive(log, tree, research_turn(research_tree()))
    view = log.view()
    saved = {(e.payload["kind"], e.payload["key"]) for e in view.events
             if e.type == "artifact.saved"}  # fmt: skip
    assert ("demo.record", "COMP-04") not in saved
    entry = TaskView(view, tree, ref("matrix")).entries("record")["COMP-04"]
    assert entry == Unknown("demo.record for COMP-04", "unverified", failed_step=COMP_04)


def test_a_blocked_key_hides_a_version_its_writer_saved() -> None:
    """A blocked writer's output is never read as a value, even when it saved one."""
    tree, log = research_tree(), TaskLog()
    log.pop("scope")
    log.select("COMP-04")
    log.save("record", "COMP-04", {"draft": True})
    log.pop(COMP_04, "fail")
    entry = TaskView(log.view(), tree, ref("matrix")).entries("record")["COMP-04"]
    assert isinstance(entry, Unknown) and entry.reason == "unverified"


def test_default_block_unchanged() -> None:
    """Without ``on_child_blocked="partial"`` the fan-out blocks, the matrix never runs."""
    tree, log = research_tree(on_child_blocked="block"), TaskLog()
    entered = drive(log, tree, research_turn(tree))
    assert "matrix" not in entered
    outcome = run_outcome(log.view(), tree, RUN)
    assert outcome is not None
    assert (outcome.outcome, outcome.blockers, outcome.unknowns) == ("blocked", (COMP_04,), ())


def test_a_reader_that_refuses_unknowns_still_blocks() -> None:
    """D3: the harness never manufactures completion; the reader's own check decides."""
    tree = research_tree(check=strict_check)
    log = TaskLog()
    entered = drive(log, tree, research_turn(tree))
    assert "matrix" in entered
    assert statuses(log.view(), tree, RUN)["matrix"] == "blocked"
    outcome = run_outcome(log.view(), tree, RUN)
    assert outcome is not None
    assert outcome.outcome == "blocked"
    assert set(outcome.blockers) == {COMP_04, "matrix"}


def test_partial_never_labelled_complete() -> None:
    """D6: every top-level Task is done, but an unknown is in the output: ``partial``."""
    tree, log = research_tree(), TaskLog()
    drive(log, tree, research_turn(tree))
    outcome = run_outcome(log.view(), tree, RUN)
    assert outcome is not None and outcome.outcome != "complete"
    clean = TaskLog()
    drive(clean, tree, research_turn(tree, fails=()))
    done = run_outcome(clean.view(), tree, RUN)
    assert done is not None and (done.outcome, done.unknowns) == ("complete", ())


def test_a_key_that_needs_a_blocked_key_is_unreached_and_named() -> None:
    """``compare[C2]`` needs ``extract[C2]``, which is blocked: it never starts, and under
    a partial rule on both stages the report runs with ``cmp/C2`` read as unknown."""
    tasks = [n.task for n in demo_tree(on_child_blocked="partial", max_finish_vetoes=1).walk()]
    tree = compile_tree(
        AGENT, (tasks[0], tasks[1], replace(tasks[2], on_child_blocked="partial"), tasks[3])
    )
    work = obedient(("C1", "C2"))
    log = TaskLog()

    def turn(pick: InstanceRef, ledger: Any) -> str:
        if pick.instance == "extract[C2]":
            return "finish"
        return work(pick, ledger)

    entered = drive(log, tree, turn)
    assert "compare[C2]" not in entered and "report" in entered
    view = log.view()
    assert statuses(view, tree, RUN)["compare[C2]"] == "new"
    assert unknowns(view, tree, RUN) == (
        UnknownEntry("extract[C2]", "unverified", "extract[C2]"),
        UnknownEntry("compare[C2]", "unverified", "extract[C2]"),
    )
    cmp = TaskView(view, tree, ref("report")).entries("cmp")
    assert isinstance(cmp["C2"], Unknown) and cmp["C2"].failed_step == "extract[C2]"
    assert not isinstance(cmp["C1"], Unknown)
    outcome = run_outcome(view, tree, RUN)
    assert outcome is not None and outcome.outcome == "partial"


def test_the_reader_context_shows_each_unknown_slot() -> None:
    """The matrix's context lists the unknown row with its reason and blocker (ADR-P08 D4)."""
    tree, log = research_tree(), TaskLog()
    drive(log, tree, research_turn(tree))
    block = render_reads(log.view(), tree, ref("matrix"), ContextPolicy(), {})
    assert "[unknown demo.record/COMP-04: unverified, blocked by fetch_extract[COMP-04]]" in (
        block.text
    )
    assert block.text.count("[artifact demo.record/") == 4
    own = render_reads(log.view(), tree, ref("fetch_extract", "COMP-01"), ContextPolicy(), {})
    assert "[unknown" not in own.text
