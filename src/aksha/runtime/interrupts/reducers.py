"""Interrupts read from the log (ADR-P17 D1): suspension is a projection, never stored.

An interrupt is open from its ``interrupt.raised`` until an ``interrupt.resolved`` (a
person answered, in the command that also wrote any kind-specific decision) or an
``interrupt.voided`` (the runtime withdrew it). Every function here is pure.

1. :func:`interrupts`: every interrupt of the owner, with its state and resolution.
2. :func:`open_interrupts`: the open ones of a run, or of one instance of it.
3. :func:`open_session_interrupts`: the open ``route`` questions of a session (S5).
4. :func:`pending_interrupts` and :func:`raised_in`: the IDs recovery (ADR-P02), the stop
   ending (ADR-P18) and the scheduler read.
5. :func:`approvals`: every approval's state (ADR-P17's state diagram), the one approval
   reducer. Invalidation (ADR-P05) reads it for revocation and the re-request after an
   accepted review; the gate, the commands and the handoff read it through
   ``approvals.py``. It lives here, beside the interrupt reducer, so invalidation can read
   it without importing the approval builders (which read the invalidation view).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Any

from aksha.runtime.interrupts.types import (
    INTERRUPT_KINDS,
    ActionBody,
    ApprovalView,
    InterruptKind,
    InterruptView,
)
from aksha.runtime.projections.view import LogView

INTERRUPT_RAISED = "interrupt.raised"
INTERRUPT_RESOLVED = "interrupt.resolved"
INTERRUPT_VOIDED = "interrupt.voided"
CLOSING = frozenset({INTERRUPT_RESOLVED, INTERRUPT_VOIDED})
"""The facts that close an interrupt."""

APPROVAL_REQUESTED = "approval.requested"
APPROVAL_GRANTED = "approval.granted"
APPROVAL_REJECTED = "approval.rejected"
APPROVAL_VOIDED = "approval.voided"
APPROVAL_REVOKED = "approval.revoked"

DETACHED_KINDS: frozenset[str] = frozenset({"review"})
"""Interrupt kinds that wait on a person without holding the run or instance that raised them.

A diff review (ADR-P05 D1) waits on the person, and the readers of the old version wait
with it (the scheduler's pending-revision rule and the rebuild plan's ``held_by``); the
writer that proposed the revision is done (F2, decision 34) and its run may end. So a
review never suspends its instance, never holds its run, is not voided when the run
ends, and is not counted by :func:`pending_interrupts`. It stays open in
:func:`interrupts` until a person accepts or rejects it, or invalidation voids it.
"""


def detached(kind: object) -> bool:
    """Whether an ``interrupt.raised`` of ``kind`` is detached from its run (above)."""
    return kind in DETACHED_KINDS


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
    """The IDs of the run's interrupts raised and not closed, in raise order. Pure.

    A detached kind (a diff review) is left out: it holds nothing of the run.
    """
    raised: list[str] = []
    closed: set[str] = set()
    for e in view.events:
        if e.run != run or (e.type != INTERRUPT_RAISED and e.type not in CLOSING):
            continue
        ident = str(e.payload.get("interrupt", e.step or ""))
        if e.type == INTERRUPT_RAISED:
            if not detached(e.payload.get("kind")):
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


def approvals(view: LogView) -> Mapping[str, ApprovalView]:
    """Every approval of the view's owner by ID, in request order, with its state. A reducer."""
    found: dict[str, ApprovalView] = {}
    for e in view.events:
        p = e.payload
        if e.type == APPROVAL_REQUESTED and "body" in p:
            ident = str(p["approval"])
            found[ident] = ApprovalView(
                ident, ActionBody.from_json(p["body"]), str(p["signature"]), "requested",
                e.run, e.instance, e.seq,
                replaces=None if p.get("replaces") is None else str(p["replaces"]),
            )  # fmt: skip
            continue
        if e.type == "outbox.enqueued":
            ident = str(p.get("approval_id", ""))
        elif e.type in (APPROVAL_GRANTED, APPROVAL_REJECTED, APPROVAL_VOIDED, APPROVAL_REVOKED):
            ident = str(p["approval"])
        else:
            continue
        held = found.get(ident)
        if held is not None:
            found[ident] = _move(held, e.type, p)
    return MappingProxyType(found)


def _move(a: ApprovalView, event_type: str, p: Mapping[str, Any]) -> ApprovalView:
    """One arrow of the state diagram; an arrow from a final state changes nothing."""
    if a.state not in ("requested", "granted"):
        return a
    if event_type == APPROVAL_GRANTED and a.state == "requested":
        return replace(a, state="granted", decided_by=str(p.get("by", "")) or None)
    if event_type == APPROVAL_REJECTED and a.state == "requested":
        return replace(a, state="rejected", decided_by=str(p.get("by", "")) or None)
    if event_type == APPROVAL_VOIDED:
        return replace(a, state="voided", void_reason=str(p.get("reason", "")) or None)
    if event_type == APPROVAL_REVOKED:
        return replace(a, state="revoked", revoked_because=tuple(p.get("moved", ())))
    if event_type == "outbox.enqueued" and a.state == "granted":
        return replace(a, state="used", used_by=str(p["key"]))
    return a
