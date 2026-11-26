"""Firing due timers (ADR-P25 D2, D3, D5) and the session's tick.

:func:`fire_due` fires each timer due at the caller's clock reading, earliest first, each
in its own transaction with what its firing writes. By cause:

==================  ====================================================  ========  =====
Cause               Firing writes                                         Boundary  Holds
==================  ====================================================  ========  =====
``approval_expiry``  ``approval.voided{expired}`` (requested or granted),  U4        no
                    the approval's own ``interrupt.voided{expired}`` if
                    open; a decided approval's timer is ``moot``
``recheck``         ``source.expired`` for its target through             U8        no
                    ``record_source_changes``, with the revocations and
                    review voids it confirms
``reminder``        ``run.started{cause: timer}`` in its session          U1        yes
==================  ====================================================  ========  =====

A firing that only voids or expires starts no work, so it asks no hold: otherwise the
open approval an expiry exists to void would hold it forever. A reminder starts work, so
it is an automatic start: it waits while ``hold_reason`` holds its agent (a person's
standing stop) or its session (an open route question), while its session has an open
run (the session's one lane), and while the agent's timer-opened runs fill the
automatic-start limit's window. A timer that waits stays pending and is asked again at
the next tick; nothing about the wait is stored.

Every close carries the dedup key ``tclose:{timer_id}``: a firing retried after a crash,
or raced by another firing path, writes nothing the second time.

:func:`tick` is what the session runs before each pass: it records the date when it can
matter (``clock.date_change``), then fires what is due.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

from aksha.runtime.budgets.limits import timer_starts
from aksha.runtime.budgets.records import TIMER_CAUSE
from aksha.runtime.budgets.types import Limits
from aksha.runtime.canonical import iso_utc
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.ports import Clock, IdSource
from aksha.runtime.interrupts.reducers import (
    APPROVAL_VOIDED,
    INTERRUPT_VOIDED,
    approvals,
    interrupts,
)
from aksha.runtime.invalidation.confirm import confirmations
from aksha.runtime.invalidation.sources import SOURCE_CHANGE_BOUNDARY, record_source_changes
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.router.decisions import session_open_runs
from aksha.runtime.stop_policy.hold import hold_reason
from aksha.runtime.stop_policy.types import AgentScope, SessionScope
from aksha.runtime.storage.errors import AlreadyRecorded
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.timers.clock import date_change
from aksha.runtime.timers.facts import cancelled, fired
from aksha.runtime.timers.types import ApprovalRef, Timer
from aksha.runtime.timers.view import due_timers

EXPIRED = "expired"
"""The reason an expired approval and its interrupt are voided (D5)."""

AGENT_UNKNOWN = "agent_unknown"
LANE_REQUIRED = "lane_required"
AUTO_START_LIMIT = "auto_start_limit"

EXPIRY_BOUNDARY = "U4"
REMINDER_BOUNDARY = "U1"

_LIVE = frozenset({"requested", "granted"})


@dataclass(frozen=True)
class FiringReport:
    """What one :func:`fire_due` pass did: fired, cancelled as moot, and waiting with why."""

    fired: tuple[str, ...] = ()
    cancelled: tuple[str, ...] = ()
    deferred: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    runs: tuple[str, ...] = ()
    date_recorded: bool = False

    @property
    def wrote(self) -> bool:
        """Whether the pass committed anything."""
        return bool(self.fired or self.cancelled or self.date_recorded)


@dataclass(frozen=True)
class _Firing:
    boundary: str
    events: tuple[NewEvent, ...]
    closed: str  # "fired" or "cancelled"
    run: str | None = None


def fire_due(
    store: EventStore,
    owner: str,
    now: datetime,
    *,
    ids: IdSource,
    agents: Mapping[str, Mapping[str, Any]] | None = None,
    limits: Limits | None = None,
    agent_limits: Mapping[str, Limits] | None = None,
) -> FiringReport:
    """Fire every timer of ``owner`` due at ``now`` (module doc); return what happened.

    ``agents`` maps each agent this runtime can start to the fields its ``run.started``
    records (definition hash, model binding, budget); a reminder for another agent waits.
    A reminder is judged against its own agent's automatic-start window: the agent's
    configured ``agent_limits`` entry, else ``limits``, else the default ``Limits()``.
    """
    default = limits or Limits()
    windows = {name: lim.auto_starts for name, lim in (agent_limits or {}).items()}
    out_fired: list[str] = []
    out_cancelled: list[str] = []
    out_runs: list[str] = []
    deferred: dict[str, str] = {}
    at = iso_utc(now)
    for timer in due_timers(load_view(store, owner), at):
        view = load_view(store, owner)
        firing = _firing(view, timer, now, ids, agents or {}, windows, default.auto_starts)
        if isinstance(firing, str):
            deferred[timer.spec.timer_id] = firing
            continue
        with contextlib.suppress(AlreadyRecorded):
            store.append(owner, firing.boundary, firing.events)
            (out_fired if firing.closed == "fired" else out_cancelled).append(timer.spec.timer_id)
            if firing.run is not None:
                out_runs.append(firing.run)
    return FiringReport(tuple(out_fired), tuple(out_cancelled), MappingProxyType(deferred),
                        tuple(out_runs))  # fmt: skip


def _firing(
    view: LogView,
    timer: Timer,
    now: datetime,
    ids: IdSource,
    agents: Mapping[str, Mapping[str, Any]],
    windows: Mapping[str, tuple[int, int]],
    default: tuple[int, int],
) -> _Firing | str:
    spec = timer.spec
    if spec.cause == "approval_expiry":
        assert isinstance(spec.scope, ApprovalRef)
        return _expiry(view, spec.timer_id, spec.scope.approval)
    if spec.cause == "recheck":
        assert spec.target is not None
        facts = record_source_changes(view, (), cause="clock", expired=(spec.target,))
        events = (*facts, *confirmations(view, facts), fired(spec.timer_id, "recheck"))
        return _Firing(SOURCE_CHANGE_BOUNDARY, events, "fired")
    assert spec.request is not None
    window = windows.get(spec.request.agent, default)
    return _reminder(view, timer, now, ids, agents, window)


def _expiry(view: LogView, timer_id: str, approval_id: str) -> _Firing:
    a = approvals(view).get(approval_id)
    if a is None or a.state not in _LIVE:
        return _Firing(EXPIRY_BOUNDARY, (cancelled(timer_id),), "cancelled")
    events: list[NewEvent] = [
        NewEvent(APPROVAL_VOIDED, {"approval": approval_id, "reason": EXPIRED},
                 run=a.run, instance=a.instance),
    ]  # fmt: skip
    i = interrupts(view).get(approval_id)
    if i is not None and i.is_open:
        events.append(NewEvent(INTERRUPT_VOIDED, {"interrupt": i.id, "reason": EXPIRED},
                               run=i.run, instance=i.instance))  # fmt: skip
    events.append(fired(timer_id, "approval_expiry"))
    return _Firing(EXPIRY_BOUNDARY, tuple(events), "fired")


def _reminder(
    view: LogView,
    timer: Timer,
    now: datetime,
    ids: IdSource,
    agents: Mapping[str, Mapping[str, Any]],
    window: tuple[int, int],
) -> _Firing | str:
    spec = timer.spec
    assert spec.request is not None and isinstance(spec.scope, SessionScope)
    agent, session = spec.request.agent, spec.scope.session
    fields = agents.get(agent)
    if fields is None:
        return AGENT_UNKNOWN
    for scope in (AgentScope(agent), SessionScope(session)):
        held = hold_reason(view, scope)
        if held is not None:
            return held.reason
    if session_open_runs(view, session):
        return LANE_REQUIRED
    count, window_ms = window
    if timer_starts(view, agent, now, window_ms) >= count:
        return AUTO_START_LIMIT
    run = ids.next(view.owner, "R")
    payload: dict[str, Any] = {**fields, "agent": agent, "cause": TIMER_CAUSE,
                               "timer": spec.timer_id, "request": spec.request.text}  # fmt: skip
    payload.setdefault("reentry", "fresh")
    started = NewEvent("run.started", payload, run=run, session=session)
    return _Firing(REMINDER_BOUNDARY, (started, fired(spec.timer_id, "reminder", run=run)),
                   "fired", run)  # fmt: skip


def tick(
    store: EventStore,
    owner: str,
    clock: Clock,
    *,
    ids: IdSource,
    agents: Mapping[str, Mapping[str, Any]] | None = None,
    limits: Limits | None = None,
    agent_limits: Mapping[str, Limits] | None = None,
) -> FiringReport:
    """Record the date if it can matter, then fire what is due (module doc)."""
    now = clock.now()
    view = load_view(store, owner)
    changed = date_change(view, now.date())
    recorded = False
    if changed:
        with contextlib.suppress(AlreadyRecorded):
            store.append(owner, SOURCE_CHANGE_BOUNDARY, (*changed, *confirmations(view, changed)))
            recorded = True
    report = fire_due(
        store, owner, now, ids=ids, agents=agents, limits=limits, agent_limits=agent_limits
    )
    if not recorded:
        return report
    return FiringReport(report.fired, report.cancelled, report.deferred, report.runs, True)
