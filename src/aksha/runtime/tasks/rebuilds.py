"""Which instances the rebuild plan re-enters in a run (ADR-P05 D7, D8, F2). Pure.

The scheduler picks them, stop-policy evaluation enters them and completion
evaluation refuses DONE to an instance the plan still lists (F2), all through
:func:`rebuild_targets`, so the three agree on one answer computed from the log.
"""

from __future__ import annotations

from aksha.runtime.invalidation.plan import plan_of, runnable
from aksha.runtime.invalidation.view import runs_for
from aksha.runtime.projections.view import LogView
from aksha.runtime.stop_policy.hold import rebuild_blocked
from aksha.runtime.tasks.errors import TaskUnknown
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import InstanceRef

REBUILD_CAUSE = "rebuild"


def is_rebuild_run(view: LogView, run: str) -> bool:
    """Whether ``run`` was opened by invalidation to rebuild invalidated work (ADR-P05 D8)."""
    for e in view.events:
        if e.type == "run.started" and e.run == run:
            return e.payload.get("cause") == REBUILD_CAUSE
    return False


def rebuild_targets(view: LogView, tree: CompiledTree, run: str) -> tuple[InstanceRef, ...]:
    """The instances of ``run`` the rebuild plan re-enters now (ADR-P05 D7, D8), oldest first.

    The plan is recomputed from the log at every call (D7). A runnable entry counts for
    ``run`` when its writer is a Task of this tree, the version it rebuilds was written for
    one of the runs ``run`` writes for (the run itself, or a rebuild run's ``for_runs``:
    the sibling rule, S1), and the hold predicate does not hold it
    (``stop_policy.hold.rebuild_blocked``, TC-4). The instance is the writer key's in
    ``run``: in an open request run that is the instance that wrote it, so the run
    regenerates its own work in place; in a rebuild run it is a fresh instance.
    """
    plan = plan_of(view)
    if not plan:
        return ()
    lines = runs_for(view)
    mine = lines.get(run, frozenset({run}))
    out: list[InstanceRef] = []
    for entry in runnable(plan):
        w = entry.writer
        if w.agent != tree.agent or entry.run is None:
            continue
        try:
            tree.node(w.task_path)
        except TaskUnknown:
            continue
        if not lines.get(entry.run, frozenset({entry.run})) & mine:
            continue
        if rebuild_blocked(view, entry) is not None:
            continue
        ref = InstanceRef(run, w.task_path, w.key)
        if ref not in out:
            out.append(ref)
    return tuple(out)
