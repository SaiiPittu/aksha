"""A research-shaped Task tree for the partial-result tests (ADR-P24).

``scope`` saves a selection of vendor IDs; ``fetch_extract`` fans out over it and saves
one ``record`` per vendor; ``matrix`` needs every ``fetch_extract`` instance taken as a
whole, reads every record and saves one row per vendor: the record's content, or the
explicit unknown the read view shows in its place. Its check is the Research matrix's
("every row holds a record or an explicit unknown"); ``strict_check`` refuses unknowns.

``research_turn`` is a scripted model for the ``tasks.drive`` helper: a vendor in
``fails`` finishes without saving its record, so its one allowed finish is refused and the
instance is blocked.
"""

from __future__ import annotations

from typing import Any

from aksha.runtime.grounding.types import Unknown
from aksha.runtime.tasks.tree import CompiledTree, compile_tree
from aksha.runtime.tasks.types import CheckResult, InstanceRef, Read, Task, artifact_keys
from aksha.runtime.tasks.view import TaskView
from tests.support.tasks import AGENT, has, has_own

VENDORS = ("COMP-01", "COMP-02", "COMP-03", "COMP-04", "COMP-05")
SELECTION = artifact_keys("selection", "ids")


def matrix_rows(view: TaskView) -> dict[str, Any]:
    """One row per key of ``record``: its content, or the unknown in its place."""
    rows: dict[str, Any] = {}
    for key, entry in view.entries("record").items():
        rows[str(key)] = entry.as_json() if isinstance(entry, Unknown) else dict(entry.content)
    return rows


def matrix_done(view: TaskView) -> CheckResult:
    """Every selected vendor has a row that matches its record or its explicit unknown."""
    selection = view.latest("selection")
    saved = view.latest("matrix")
    if selection is None or saved is None:
        return CheckResult(False, "save the matrix")
    rows = saved.content.get("rows", {})
    wanted = matrix_rows(view)
    missing = [v for v in selection.content["ids"] if v not in wanted or rows.get(v) != wanted[v]]
    return CheckResult(not missing, f"rows missing or stale: {missing}", keys=tuple(missing))


def strict_check(view: TaskView) -> CheckResult:
    """A reader that refuses unknowns (ADR-P24 D3): every vendor needs a real record."""
    found = matrix_done(view)
    if not found.ok:
        return found
    gaps = [str(k) for k, e in view.entries("record").items() if isinstance(e, Unknown)]
    return CheckResult(not gaps, f"no record for {gaps}", keys=tuple(gaps))


def research_tree(
    *, on_child_blocked: str = "partial", finalizer: bool = True, check: Any = matrix_done
) -> CompiledTree:
    """The compiled research tree (module doc)."""
    return compile_tree(
        AGENT,
        (
            Task("scope", writes=("selection",), done=has("selection")),
            Task(
                "fetch_extract",
                per=SELECTION,
                writes=("record",),
                done=has_own("record"),
                max_finish_vetoes=1,
                on_child_blocked=on_child_blocked,  # type: ignore[arg-type]
            ),
            Task(
                "matrix",
                needs=("fetch_extract",),
                reads=("selection", Read("record", keys="all")),
                writes=("matrix",),
                done=check,
                finalizer=finalizer,
            ),
        ),
    )


def research_turn(tree: CompiledTree, fails: tuple[str, ...] = ("COMP-04",)) -> Any:
    """A scripted model for ``tasks.drive`` over :func:`research_tree` (module doc)."""

    def turn(pick: InstanceRef, ledger: Any) -> str:
        if pick.task_path == "scope":
            ledger.select(*VENDORS)
        elif pick.task_path == "fetch_extract" and pick.key not in fails:
            ledger.save("record", pick.key, {"vendor": pick.key, "price": 100})
        elif pick.task_path == "matrix":
            rows = matrix_rows(TaskView(ledger.view(), tree, pick))
            ledger.save("matrix", None, {"rows": rows})
        return "finish"

    return turn
