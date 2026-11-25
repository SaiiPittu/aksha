"""Open interrupts read from the log (ADR-P17 D1): suspension is a projection, never stored.

Built so far: :func:`pending_interrupts`, which recovery (ADR-P02) and the stop ending
(ADR-P18) read. An interrupt is open from its ``interrupt.raised`` until an
``interrupt.resolved`` (a person answered) or an ``interrupt.voided`` (the runtime withdrew
it). ``interrupt.raised`` and ``interrupt.resolved`` are read by name; no core type declares
them until the interrupt primitive is built.
"""

from __future__ import annotations

from aksha.runtime.projections.view import LogView

INTERRUPT_RAISED = "interrupt.raised"
INTERRUPT_RESOLVED = "interrupt.resolved"
INTERRUPT_VOIDED = "interrupt.voided"
CLOSING = frozenset({INTERRUPT_RESOLVED, INTERRUPT_VOIDED})
"""The facts that close an interrupt."""


def pending_interrupts(view: LogView, run: str) -> tuple[str, ...]:
    """The IDs of the run's interrupts raised and not closed, in raise order. Pure."""
    raised: list[str] = []
    closed: set[str] = set()
    for e in view.events:
        if e.run != run or (e.type != INTERRUPT_RAISED and e.type not in CLOSING):
            continue
        ident = str(e.payload.get("interrupt", e.step or ""))
        if e.type == INTERRUPT_RAISED:
            raised.append(ident)
        else:
            closed.add(ident)
    return tuple(i for i in raised if i not in closed)


def raised_in(view: LogView, runs: frozenset[str]) -> frozenset[str]:
    """The IDs of every interrupt raised in one of ``runs``, open or closed. Pure."""
    return frozenset(
        str(e.payload.get("interrupt", e.step or ""))
        for e in view.events
        if e.type == INTERRUPT_RAISED and e.run in runs
    )
