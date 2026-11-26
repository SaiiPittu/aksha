"""The hold predicate (ADR-P16 D6, S1) and the audit that proves it (D7). Every function is pure.

``hold_reason(view, scope)`` is the one function every automatic start path asks before
it writes: stop-policy evaluation (box 3, before any policy may propose work), the Run
policy before it pushes a pick, and the rebuild-run opener (through ``rebuild_blocked``).
Recovery asks the stop part (``cancellation.hold.stop_hold``) directly.

S1, the hold covers the held work, not the whole agent:

==================  ===========================================================
Scope               Held by
==================  ===========================================================
an ``InstanceRef``  its run's hold, or an open interrupt raised by that instance
``RunScope``        a pending stop request, or an open run-scoped interrupt (the
                    budget interrupt); never one instance's interrupt
``AgentScope``      a person's standing stop (``stopped_by_person``) only
``SessionScope``    an open session-scoped interrupt (the request router's
                    ``route`` question, ADR-P17 S5): no run opens for the message
==================  ===========================================================

So an approval pending on one instance parks that instance; the run's other instances,
and writers upstream of it, go on. A diff review holds nothing here
(``interrupts.reducers.DETACHED_KINDS``): its readers wait through the scheduler and the
rebuild plan instead, and its writer is done (F2).

``held_auto_starts`` is ``inv.no_auto_start_while_held``: it walks a whole log and returns
every automatic start recorded while its scope was held, by a stop (``cancellation``) or by
an open interrupt (here). An empty result is the invariant holding.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

from aksha.runtime.budgets.records import AUTOMATIC_CAUSES, TIMER_CAUSE
from aksha.runtime.cancellation.hold import held_auto_starts as stop_held_starts
from aksha.runtime.cancellation.hold import stop_hold
from aksha.runtime.cancellation.requests import pending_stop
from aksha.runtime.cancellation.types import HeldStart
from aksha.runtime.events.envelope import Event
from aksha.runtime.interrupts.reducers import CLOSING, INTERRUPT_RAISED, detached
from aksha.runtime.invalidation.types import RebuildEntry
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS, open_runs
from aksha.runtime.projections.view import LogView
from aksha.runtime.stop_policy.types import AgentScope, Hold, RunScope, SessionScope
from aksha.runtime.tasks.types import InstanceRef

HoldScope = InstanceRef | RunScope | AgentScope | SessionScope
"""What may be held (S1)."""

INTERRUPT_OPEN: Final = "interrupt_open"


@dataclass
class _Open:
    """Open interrupts while walking a log once: (run, instance or None) to (id, seq)."""

    raised: dict[str, tuple[str, str | None, int]] = field(default_factory=dict)
    sessions: dict[str, tuple[str, int]] = field(default_factory=dict)

    def apply(self, e: Event) -> None:
        ident = str(e.payload.get("interrupt", e.step or ""))
        if e.type == INTERRUPT_RAISED and detached(e.payload.get("kind")):
            return  # a diff review holds no run and no instance (ADR-P05 D1, F2)
        if e.type == INTERRUPT_RAISED and e.run is not None:
            self.raised[ident] = (e.run, e.instance, e.seq)
        elif e.type == INTERRUPT_RAISED and e.session is not None:
            self.sessions[ident] = (e.session, e.seq)
        elif e.type in CLOSING:
            self.raised.pop(ident, None)
            self.sessions.pop(ident, None)

    def in_session(self, session: str) -> int | None:
        found = [seq for s, seq in self.sessions.values() if s == session]
        return min(found) if found else None

    def first(self, run: str, instance: str | None) -> tuple[str, int] | None:
        found = [(seq, i) for i, (r, at, seq) in self.raised.items() if (r, at) == (run, instance)]
        if not found:
            return None
        seq, ident = min(found)
        return ident, seq


def _open_interrupts(view: LogView) -> _Open:
    state = _Open()
    for e in view.events:
        state.apply(e)
    return state


def open_interrupt(view: LogView, run: str, instance: str | None) -> tuple[str, int] | None:
    """The oldest open interrupt raised by ``instance`` of ``run`` (None: run-scoped), or None.

    Returns ``(interrupt id, seq of its interrupt.raised)``.
    """
    return _open_interrupts(view).first(run, instance)


def hold_reason(view: LogView, scope: HoldScope) -> Hold | None:
    """Why human state holds ``scope`` now, or None (module doc). Stop first, then interrupts."""
    if isinstance(scope, AgentScope):
        held = stop_hold(view, agent=scope.agent)
        return None if held is None else Hold(held.reason, held.by, scope.agent)
    if isinstance(scope, SessionScope):
        seq = _open_interrupts(view).in_session(scope.session)
        return None if seq is None else Hold(INTERRUPT_OPEN, seq, scope.session)
    run = scope.run
    pending = pending_stop(view, run)
    if pending is not None:
        return Hold("stop_requested", pending.seq, run)
    opened = _open_interrupts(view)
    found = opened.first(run, None)
    if found is not None:
        return Hold(INTERRUPT_OPEN, found[1], run)
    if isinstance(scope, InstanceRef):
        found = opened.first(run, scope.instance)
        if found is not None:
            return Hold(INTERRUPT_OPEN, found[1], str(scope))
    return None


def rebuild_blocked(view: LogView, entry: RebuildEntry) -> Hold | None:
    """S1: why one rebuild entry may not start now, or None.

    Held when the writer's agent is held by a person's stop, or when the writer's instance
    in an open run of the agent is held (its interrupt is open, or its run is stopping).
    Other entries of the same agent are not held by it.
    """
    writer = entry.writer
    held = hold_reason(view, AgentScope(writer.agent))
    if held is not None:
        return held
    runs = rebuild(RUNS, view)
    for run in open_runs(runs):
        if runs[run].agent != writer.agent:
            continue
        held = hold_reason(view, InstanceRef(run, writer.task_path, writer.key))
        if held is not None:
            return held
    return None


def _interrupt_starts(view: LogView) -> Iterator[HeldStart]:
    """Automatic starts recorded while an open interrupt held their scope (S1 scopes)."""
    opened = _Open()
    for e in view.events:
        held = _held_by_interrupt(opened, e)
        if held:
            yield HeldStart(e.seq, e.type, e.run, "interrupt_open")
        opened.apply(e)


def _held_by_interrupt(opened: _Open, e: Event) -> bool:
    if e.type == "run.started" and e.payload.get("cause") == TIMER_CAUSE:
        # ADR-P25 D2: a reminder's run is held by an open question of its session.
        return e.session is not None and opened.in_session(e.session) is not None
    if e.run is None or e.instance is None:
        return False
    p = e.payload
    if e.type == "stop.decided":
        if p.get("verdict") != "continue" or p.get("cause") not in AUTOMATIC_CAUSES:
            return False
    elif e.type == "step.started":
        if p.get("kind") != "model":
            return False
    elif e.type != "task.entered":
        return False
    return opened.first(e.run, None) is not None or opened.first(e.run, e.instance) is not None


def held_auto_starts(view: LogView) -> tuple[HeldStart, ...]:
    """TC-4, ``inv.no_auto_start_while_held``: every automatic start made while held.

    The stop's half is ``cancellation.hold.held_auto_starts``; the interrupt half judges
    a continuing stop decision with an automatic cause, a ``task.entered`` and a model
    ``step.started``, each on the instance it names, and a timer's ``run.started`` on its
    session (ADR-P25 D2), against the interrupts open just before it. Sorted by seq.
    """
    found = {(h.seq, h.reason): h for h in stop_held_starts(view)}
    for h in _interrupt_starts(view):
        found.setdefault((h.seq, h.reason), h)
    return tuple(found[k] for k in sorted(found))
