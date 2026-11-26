"""Partial results and finalization (ADR-P24): explicit unknowns instead of a stopped run.

Built:

1. ``types.py``: ``UnknownEntry``, ``Finalizing`` and the reserve's placeholder shares.
2. ``state.py``: whether a run is finalizing, read from ``run.finalizing`` (D4).
3. ``unknowns.py``: explicit unknowns as a projection: blocked keys a partial rule
   tolerates read ``unverified`` (D2), voided keys ``not_checked`` (D4), the readers'
   ``entries`` and the unknown slots a Task's context shows.
4. ``reserve.py``: the reserved budget held back from work that is not a finalizer (D5).
5. ``finalize.py``: the facts of the budget interrupt's ``finalize`` answer (D4); the
   command is ``interrupts.commands.finalize``.

Elsewhere: ``on_child_blocked`` and ``finalizer`` on ``Task``; unreached and voided
instances in ``tasks/status.py``; the finalizer-only pick in ``tasks/scheduler.py``; the
run outcome (``complete``, ``partial`` or ``blocked``, D6) with its unknowns in
``run.ended``; the reserve-spent ending in ``stop_policy/core.py``.

Planned: a per-finalizer declared reserve (PF-2, option B); whether a partial result
passes an eval (ADR-P22).
"""
