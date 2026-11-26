"""The Task model and scheduler on a real SQLite event store (ADR-P15 Tests).

Every fact the scheduler and completion evaluation build is appended through the store,
so the catalogue's schemas and transaction boundaries accept them; statuses are read back
from the store, including after the store is closed and opened again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.finalization.types import UnknownEntry
from aksha.runtime.projections.view import load_view
from aksha.runtime.storage.chain import content_hash
from aksha.runtime.tasks.completion import evaluate_completion, reset_fact
from aksha.runtime.tasks.scheduler import run_outcome
from aksha.runtime.tasks.status import statuses
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import CheckResult, InstanceRef, Task
from aksha.runtime.tasks.view import TaskView
from tests.support.tasks import AGENT, StoreLedger, demo_tree, drive, has, obedient, ref

RUN = "R-0001"


def test_s1_order_through_the_store(tmp_path: Path) -> None:
    ledger = StoreLedger(tmp_path / "aksha.sqlite")
    entered = drive(ledger, demo_tree(), obedient(("C2", "C1")))
    assert entered == [
        "scope", "extract[C1]", "extract[C2]", "compare[C1]", "compare[C2]", "report",
    ]  # fmt: skip
    outcome = run_outcome(ledger.view(), demo_tree(), RUN)
    assert outcome is not None and outcome.outcome == "complete"
    assert ledger.store.verify() is None


def test_status_equals_full_rebuild(tmp_path: Path) -> None:
    """At every stop decision, and after a restart, statuses equal a rebuild from seq 0."""
    ledger = StoreLedger(tmp_path / "aksha.sqlite")
    tree = demo_tree()
    seen: list[tuple[int, dict[str, str]]] = []

    def record(current: Any) -> None:
        view = current.view()
        seen.append((view.seq_read, dict(statuses(view, tree, RUN))))

    drive(ledger, tree, obedient(("C1", "C2")), on_decision=record)
    reopened = ledger.reopen()
    full = load_view(reopened.store, reopened.owner)
    for seq, before in seen:
        assert dict(statuses(full.upto(seq), tree, RUN)) == before
    assert set(statuses(full, tree, RUN).values()) == {"done"}


def test_three_vetoes_then_blocked_and_the_run_ends_blocked(tmp_path: Path) -> None:
    ledger = StoreLedger(tmp_path / "aksha.sqlite")
    work = obedient(("C1", "C2"))
    turns: list[str] = []

    def stubborn(pick: InstanceRef, current: Any) -> str:
        turns.append(pick.instance)
        if pick.instance == "extract[C2]":
            return "finish"  # claims done without saving the item
        return work(pick, current)

    tree = demo_tree(on_child_blocked="partial")
    drive(ledger, tree, stubborn)
    assert turns.count("extract[C2]") == 3
    view = ledger.view()
    assert statuses(view, tree, RUN)["extract[C2]"] == "blocked"
    assert statuses(view, tree, RUN)["compare[C1]"] == "done"
    outcome = run_outcome(view, tree, RUN)
    assert outcome is not None
    # ADR-P24: extract tolerates its blocked key, but compare[C2] can never start and
    # compare keeps the default rule, so the block reaches the run: ``blocked``.
    assert (outcome.outcome, outcome.blockers) == ("blocked", ("extract[C2]",))
    assert outcome.unknowns == (UnknownEntry("extract[C2]", "unverified", "extract[C2]"),)


def test_insist_does_not_unblock(tmp_path: Path) -> None:
    ledger = StoreLedger(tmp_path / "aksha.sqlite")
    tree = compile_tree(AGENT, (Task("packet", done=has("packet"), max_finish_vetoes=1),))
    drive(ledger, tree, lambda pick, current: "finish")
    assert statuses(ledger.view(), tree, RUN)["packet"] == "blocked"
    for n, text in enumerate(("mark it complete", "I insist, it is done"), start=1):
        payload = {"command_id": f"c{n}", "name": "message", "args_hash": content_hash(text)}
        ledger.commit([NewEvent("command.accepted", payload, run=RUN)], boundary="U1")
    # A person's message is a command, never a status fact: only a reset or an entry moves it.
    assert statuses(ledger.view(), tree, RUN)["packet"] == "blocked"
    ledger.commit([reset_fact(ref("packet"), "answer")], boundary="U4")
    assert statuses(ledger.view(), tree, RUN)["packet"] == "running"


class Forbidden:
    """Stands for the store, the clock and the model: any use raises."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"a completion check reached for {name}")


def test_checks_are_pure(tmp_path: Path) -> None:
    """Every demo check runs on a ``TaskView``, which holds no store, clock or model."""
    ledger = StoreLedger(tmp_path / "aksha.sqlite")
    tree = demo_tree()
    drive(ledger, tree, obedient(("C1",)))
    view = ledger.view()
    ledger.close()
    ledger.store = Forbidden()
    ledger.clock = Forbidden()  # type: ignore[assignment]
    for node in tree.walk():
        key = "C1" if node.task.per is not None else None
        first = evaluate_completion(view, tree, ref(node.path, key))
        assert first == evaluate_completion(view, tree, ref(node.path, key))
        assert first.ok


def suite_passes(view: TaskView) -> CheckResult:
    """D4: the latest report has no failures and ran against the current files."""
    report = view.latest("test_report")
    files = view.latest("files")
    if report is None or files is None:
        return CheckResult(ok=False, message="run the tests")
    if report.content["files_version"] != files.ref.version:
        return CheckResult(ok=False, message="the files changed since the last test run")
    failing = int(report.content["failures"])
    return CheckResult(ok=failing == 0, message=f"{failing} tests fail", count=failing)


def test_stale_report_fails_check(tmp_path: Path) -> None:
    ledger = StoreLedger(tmp_path / "aksha.sqlite")
    tree = compile_tree(AGENT, (Task("fix", writes=("files", "test_report"), done=suite_passes),))
    fix = ref("fix")
    ledger.save("files", None, {"a.py": "x = 1"})
    ledger.save("test_report", None, {"files_version": 1, "failures": 0})
    assert evaluate_completion(ledger.view(), tree, fix).ok
    ledger.save("files", None, {"a.py": "x = 2"})
    stale = evaluate_completion(ledger.view(), tree, fix)
    assert (stale.ok, stale.message) == (False, "the files changed since the last test run")
    ledger.save("test_report", None, {"files_version": 2, "failures": 0})
    assert evaluate_completion(ledger.view(), tree, fix).ok
