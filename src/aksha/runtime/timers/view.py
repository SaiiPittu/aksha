"""The timer projection (ADR-P25 D1): every timer, its due time and its state. Pure.

A timer's due time is its ``due_at``, or its scheduling fact's recorded ``at`` plus
``after_ms`` (amendment B2). Both come from the injected clock, and ``due`` compares them
with the caller's clock reading, never a clock of its own, so a fake run fires the same
timers at the same point every time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from types import MappingProxyType

from aksha.runtime.canonical import iso_utc
from aksha.runtime.projections.view import LogView
from aksha.runtime.timers.facts import (
    TIMER_CANCELLED,
    TIMER_FIRED,
    TIMER_SCHEDULED,
    timer_from_json,
)
from aksha.runtime.timers.types import Timer, TimerSet, TimerState


def _due_at(spec: TimerSet, at: str) -> str:
    if spec.due_at is not None:
        return iso_utc(datetime.fromisoformat(spec.due_at))
    assert spec.after_ms is not None
    return iso_utc(datetime.fromisoformat(at) + timedelta(milliseconds=spec.after_ms))


def timers(view: LogView) -> Mapping[str, Timer]:
    """Every timer of the view's owner by ID, in scheduling order. A reducer."""
    found: dict[str, Timer] = {}
    for e in view.events:
        if e.type == TIMER_SCHEDULED:
            spec = timer_from_json(e.payload)
            found[spec.timer_id] = Timer(spec, _due_at(spec, e.at), e.seq)
        elif e.type in (TIMER_FIRED, TIMER_CANCELLED):
            ident = str(e.payload["timer_id"])
            held = found.get(ident)
            if held is not None and held.pending:
                state: TimerState = "fired" if e.type == TIMER_FIRED else "cancelled"
                found[ident] = replace(held, state=state, closed_seq=e.seq)
    return MappingProxyType(found)


def pending(view: LogView) -> tuple[Timer, ...]:
    """The timers not yet fired or cancelled, in scheduling order."""
    return tuple(t for t in timers(view).values() if t.pending)


def due_timers(view: LogView, now: str) -> tuple[Timer, ...]:
    """The pending timers due at ``now`` (an ISO time), earliest first, then by schedule."""
    at = datetime.fromisoformat(now)
    ready = [t for t in pending(view) if datetime.fromisoformat(t.due_at) <= at]
    return tuple(sorted(ready, key=lambda t: (datetime.fromisoformat(t.due_at), t.scheduled_seq)))


def due(view: LogView, now: str) -> tuple[TimerSet, ...]:
    """The ADR's projection: the specs of the timers due at ``now``, earliest first."""
    return tuple(t.spec for t in due_timers(view, now))
