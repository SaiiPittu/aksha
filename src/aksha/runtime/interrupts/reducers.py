"""Interrupts read from the log (ADR-P17 D1): suspension is a projection, never stored.

An interrupt is open from its ``interrupt.raised`` until an ``interrupt.resolved`` (a
person answered, in the command that also wrote any kind-specific decision) or an
``interrupt.voided`` (the runtime withdrew it). Every function here is pure.

1. :func:`interrupts`: every interrupt of the owner, with its state and resolution.
2. :func:`open_interrupts`: the open ones of a run, or of one instance of it.
3. :func:`open_session_interrupts`: the open ``route`` questions of a session (S5).
4. :func:`pending_interrupts` and :func:`raised_in`: the IDs recovery (ADR-P02), the stop
   ending (ADR-P18) and the scheduler read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType

from aksha.runtime.interrupts.types import INTERRUPT_KINDS, InterruptKind, InterruptView
from aksha.runtime.projections.view import LogView

INTERRUPT_RAISED = "interrupt.raised"
INTERRUPT_RESOLVED = "interrupt.resolved"
INTERRUPT_VOIDED = "interrupt.voided"
CLOSING = frozenset({INTERRUPT_RESOLVED, INTERRUPT_VOIDED})
"""The facts that close an interrupt."""

_ENVELOPE_FIELDS = frozenset({"interrupt", "kind"})


def _kind(value: object) -> InterruptKind:
    """A stored kind; an interrupt raised before kinds were recorded reads as ``input``."""
    text = str(value) if value is not None else "input"
    return text if text in INTERRUPT_KINDS else "input"


def interrupts(view: LogView) -> Mapping[str, InterruptView]:
    """Every interrupt of the view's owner by ID, in raise order, with its state. A reducer."""
    found: dict[str, InterruptView] = {}
    for e in view.events:
        if e.type not in CLOSING and e.type != INTERRUPT_RAISED:
            continue
        ident = str(e.payload.get("interrupt", e.step or ""))
        if e.type == INTERRUPT_RAISED:
            shown = {k: v for k, v in e.payload.items() if k not in _ENVELOPE_FIELDS}
            found[ident] = InterruptView(
                ident, _kind(e.payload.get("kind")), e.run, e.instance, e.session,
                MappingProxyType(shown), e.seq,
            )  # fmt: skip
            continue
        held = found.get(ident)
        if held is None or not held.is_open:
            continue
        if e.type == INTERRUPT_RESOLVED:
            found[ident] = replace(held, state="resolved", resolution=e.payload)
        else:
            found[ident] = replace(held, state="voided", void_reason=str(e.payload["reason"]))
    return MappingProxyType(found)


def open_interrupts(
    view: LogView, run: str, instance: str | None = None
) -> tuple[InterruptView, ...]:
    """The run's open interrupts, in raise order; with ``instance``, only that instance's."""
    return tuple(
        i
        for i in interrupts(view).values()
        if i.is_open and i.run == run and (instance is None or i.instance == instance)
    )


def open_session_interrupts(view: LogView, session: str) -> tuple[InterruptView, ...]:
    """The open session-scoped interrupts (``route``) of ``session``, in raise order."""
    return tuple(
        i
        for i in interrupts(view).values()
        if i.is_open and i.scope == "session" and i.session == session
    )


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
