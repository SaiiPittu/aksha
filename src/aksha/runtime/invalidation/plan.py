"""The rebuild plan (ADR-P05 D7, S4) and the freshness condition F2. Every function is pure.

The plan is the invalidation view asked "what is invalidated and has nobody waiting on a
person". It is recomputed after every commit:

1. Take every latest version that is invalidated, oldest first.
2. Skip one with an open diff review (its revision waits on a person), one whose last
   review was rejected (S4: the writer is blocked, ``review_rejected``, until the person
   chooses again), and one whose writer instance is withdrawn.
3. Choose the mode: ``copy_choice`` for a version whose content a person chose (origin
   ``choice`` or ``copy_choice``), ``propose`` for any other human-reviewed version, and
   ``regenerate`` for a machine-only one. ``rebase`` (S5) is designed, not built.
4. Hold a writer while one of its other reads is unsettled: an input that is itself
   invalidated, dirty or under an open diff review. A held entry is listed with
   ``held_by`` and is not entered; it settles when its inputs do. Dirty versions are
   never entered: they settle when their inputs do.

F2 (decision 34): an instance is not done while the plan lists one of its latest
versions as runnable. A ``propose`` writer that has written its pending revision is done,
because step 2 skips a version with an open review.
"""

from __future__ import annotations

from aksha.runtime.artifacts.types import WriterId
from aksha.runtime.invalidation.types import RebuildEntry, RebuildMode, VersionState
from aksha.runtime.invalidation.view import HUMAN_ORIGINS, InvalidationView


def rebuild_plan(iv: InvalidationView) -> tuple[RebuildEntry, ...]:
    """The rebuild plan of ``iv`` (module doc), oldest first, held entries included."""
    out: list[RebuildEntry] = []
    for state in iv.invalidated_latest():
        ref = str(state.ref)
        if state.open_revision is not None or ref in iv.rejected:
            continue
        if state.writer in iv.withdrawn:
            continue
        held = tuple(str(r.input) for r in state.reads if r.state == "unsettled")
        out.append(
            RebuildEntry(
                writer=state.writer,
                mode=mode_of(state),
                invalidated=state.ref,
                because=state.changed_reads(),
                run=state.run,
                held_by=held,
            )
        )
    return tuple(out)


def mode_of(state: VersionState) -> RebuildMode:
    """How ``state``'s writer rebuilds it (module doc, step 3)."""
    if state.origin in HUMAN_ORIGINS:
        return "copy_choice"
    return "propose" if state.human_reviewed else "regenerate"


def runnable(plan: tuple[RebuildEntry, ...]) -> tuple[RebuildEntry, ...]:
    """The entries that may be entered now."""
    return tuple(e for e in plan if e.runnable)


def writer_has_rebuild(plan: tuple[RebuildEntry, ...], writer: WriterId) -> bool:
    """F2: whether ``writer`` is not done because the plan lists one of its latest versions."""
    return any(e.writer == writer for e in runnable(plan))
