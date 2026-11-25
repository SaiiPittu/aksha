"""The stop ending at a stop candidate (ADR-P18 D2, D3).

When a run with a pending stop reaches a stop candidate of any kind, the Run policy's
answer is STOP with outcome ``stopped``, before any inner policy can propose a turn. One
transaction (U3) writes, in order:

1. ``interrupt.voided{reason: run_stopped}`` for each interrupt of the run still open;
2. ``run.ended{outcome: stopped, reason: stop_requested}``;
3. ``stop.decided{verdict: stop, reason: stop_requested}``, last, naming the candidate.

Approvals keep their own lifecycle: a pending approval stays decidable (ADR-P17 D8).
:func:`stop_ending` is the pure decision; :func:`honour_stop` commits it. Their callers are
recovery's resume candidate (``resume``, D3) and the end of a turn a resumed driver returns
from (``turn_end``). ADR-P16's evaluation answers every other candidate with the Run
policy's STOP ``stopped`` before any policy judges, and commits :func:`ending_facts` with
its own, fuller record.
"""

from __future__ import annotations

import contextlib

from aksha.runtime.cancellation.requests import STOP_DECIDED, pending_stop
from aksha.runtime.cancellation.types import RUN_STOPPED, STOP_REQUESTED, STOPPED
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.interrupts.reducers import INTERRUPT_VOIDED, pending_interrupts
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.storage.errors import AlreadyRecorded
from aksha.runtime.storage.ports import EventStore

STOP_DECISION = "U3"
"""The boundary of the stop ending (a stop decision)."""


def ending_facts(view: LogView, run: str) -> tuple[NewEvent, ...]:
    """Items 1 and 2 of the module doc: the voided interrupts and ``run.ended{stopped}``.

    Stop-policy evaluation (ADR-P16) commits these before its own, fuller record.
    """
    voided = tuple(
        NewEvent(INTERRUPT_VOIDED, {"interrupt": i, "reason": RUN_STOPPED}, run=run)
        for i in pending_interrupts(view, run)
    )
    ended = NewEvent("run.ended", {"outcome": STOPPED, "reason": STOP_REQUESTED}, run=run)
    return (*voided, ended)


def stop_ending(view: LogView, run: str, candidate: str) -> tuple[NewEvent, ...]:
    """The events that end ``run`` at ``candidate`` for its pending stop, or () if none. Pure."""
    if pending_stop(view, run) is None:
        return ()
    walk = 1 + sum(1 for e in view.events if e.run == run and e.type == STOP_DECIDED)
    decided = NewEvent(
        STOP_DECIDED,
        {"walk": walk, "verdict": "stop", "cause": candidate, "candidate": candidate,
         "reason": STOP_REQUESTED},
        run=run,
    )  # fmt: skip
    return (*ending_facts(view, run), decided)


def honour_stop(store: EventStore, owner: str, run: str, candidate: str) -> bool:
    """Commit the stop ending of ``run`` if a stop is pending; True when it was written.

    An ending already recorded (another path honoured it first) writes nothing.
    """
    events = stop_ending(load_view(store, owner), run, candidate)
    if not events:
        return False
    with contextlib.suppress(AlreadyRecorded):
        store.append(owner, STOP_DECISION, events)
        return True
    return False
