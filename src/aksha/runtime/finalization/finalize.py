"""Finalization (ADR-P24 D4): the budget interrupt's third answer, as facts. Pure.

``finalize_facts`` builds what the ``finalize`` command commits beside the interrupt's
``interrupt.resolved{decision: finalized}`` (U4, ``interrupts.commands.finalize``):

1. ``run.finalizing{interrupt, by, reserve, finalizers, voided}``: from here on only
   finalizer Tasks (and the Tasks above and below them) may run, in declared order, and
   every other instance not yet settled is voided, its output ``not_checked``;
2. ``interrupt.voided{reason: finalizing}`` for each open interrupt of a voided instance,
   so nobody is asked about work that will not run.

Finalization is an automatic start like any other (D7): each finalizer is picked by the
Run stop policy, which calls the hold predicate first, so a person's stop still wins.
"""

from __future__ import annotations

from aksha.runtime.budgets.meter import meter
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.finalization.reserve import finalizer_paths, reserve_for
from aksha.runtime.finalization.state import RUN_FINALIZING
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.status import all_instances, run_state
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import SETTLED

FINALIZING_REASON = "finalizing"
INTERRUPT_VOIDED = "interrupt.voided"


def to_void(view: LogView, tree: CompiledTree, run: str) -> tuple[str, ...]:
    """The instances finalization voids now: not settled, and not exempt (``tasks.status``)."""
    state = run_state(view, tree, run)
    return tuple(
        r.instance
        for r in all_instances(state)
        if state.status(r) not in SETTLED and not state.exempt(r.task_path)
    )


def finalize_facts(
    view: LogView, tree: CompiledTree, run: str, interrupt: str, by: str
) -> tuple[NewEvent, ...]:
    """``run.finalizing`` and the voids it implies (module doc). Writes nothing."""
    state = run_state(view, tree, run)
    voided = to_void(view, tree, run)
    reserve = reserve_for(tree, meter(view, run).allowed)
    payload = {
        "interrupt": interrupt,
        "by": by,
        "reserve": dict(reserve),
        "finalizers": list(finalizer_paths(tree)),
        "voided": list(voided),
    }
    out = [NewEvent(RUN_FINALIZING, payload, run=run)]
    for instance in voided:
        for ident in state.open_interrupts.get(instance, ()):
            body = {"interrupt": ident, "reason": FINALIZING_REASON}
            out.append(NewEvent(INTERRUPT_VOIDED, body, run=run, instance=instance))
    return tuple(out)
