"""The stop's holds and the TC-4 audit (ADR-P18 D2, D4; ADR-P16 D6, D7). Every function is pure.

Two holds come from a person's stop:

1. ``stop_requested`` holds a run from its stop request until the stop ending; no new step
   of it starts and no stop decision may continue it.
2. ``stopped_by_person`` holds an (owner, agent) after the person's stop ended one of its
   runs, until that person's next command naming the agent: a new run they ask for
   (``run.started{cause: request}``) or an answer to one of the agent's interrupts
   (``command.accepted`` with ``answers``). A view is one owner's log, so the owner is the
   view's.

Every automatic start path asks :func:`stop_hold` before it writes: ADR-P16's
``stop_policy.hold.hold_reason`` adds ``interrupt_open`` and calls this for the rest, and
recovery calls this directly. :func:`held_auto_starts` is the stop half of the audit
``inv.no_auto_start_while_held``: it walks a whole log and returns every automatic start
recorded while its scope was held by a stop, so a path that forgot the call is caught;
``stop_policy.hold.held_auto_starts`` joins it with the interrupt half.
"""

from __future__ import annotations

from aksha.runtime.budgets.records import AUTOMATIC_CAUSES
from aksha.runtime.cancellation.requests import pending_stop, stop_requests
from aksha.runtime.cancellation.types import (
    STOPPED,
    HeldStart,
    StopHold,
    StopRequestView,
)
from aksha.runtime.events.envelope import Event
from aksha.runtime.interrupts.reducers import raised_in
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS, RunsState
from aksha.runtime.projections.view import LogView

PERSON_CAUSE = "request"
"""The ``run.started`` cause of a run a person asked for; every other cause is automatic."""


def standing_hold(view: LogView, agent: str) -> StopRequestView | None:
    """D4: the request behind the agent's last stopped run, until the person's next command."""
    runs = rebuild(RUNS, view)
    mine = frozenset(r for r, s in runs.items() if s.agent == agent)
    request: StopRequestView | None = None
    since = -1
    for run in sorted(mine):
        if runs[run].outcome != STOPPED:
            continue
        for r in stop_requests(view, run):
            if r.honoured_by is not None and r.honoured_by >= since:
                request, since = r, r.honoured_by
    if request is None:
        return None
    answerable = raised_in(view, mine)
    for e in view.events:
        if e.seq > since and _names_agent(e, agent, answerable):
            return None
    return request


def _names_agent(e: Event, agent: str, answerable: frozenset[str]) -> bool:
    """Whether ``e`` is a person's command naming ``agent``: a new run, or an answer."""
    p = e.payload
    if e.type == "run.started":
        return p.get("agent") == agent and p.get("cause") == PERSON_CAUSE
    return e.type == "command.accepted" and p.get("answers") in answerable


def stop_hold(
    view: LogView, *, run: str | None = None, agent: str | None = None
) -> StopHold | None:
    """Why ``run`` or ``agent`` is held by a person's stop, or None. The run is asked first."""
    if run is not None:
        pending = pending_stop(view, run)
        if pending is not None:
            return StopHold("stop_requested", run, pending.seq)
    if agent is not None:
        standing = standing_hold(view, agent)
        if standing is not None:
            return StopHold("stopped_by_person", agent, standing.seq)
    return None


def held_auto_starts(view: LogView) -> tuple[HeldStart, ...]:
    """TC-4: every automatic start ``view`` records while a stop held its scope.

    An automatic start is a ``run.started`` whose cause is not a person's request (held by
    its agent), a ``stop.decided`` that continues with an automatic cause (held by its run
    or its run's agent), or any ``step.started`` (held by its run, D5). Each is judged on
    the log as it stood just before it. An empty result is the invariant holding.
    """
    runs = rebuild(RUNS, view)
    found: list[HeldStart] = []
    for e in view.events:
        scope = _start_scope(e, runs)
        if scope is None:
            continue
        run, agent = scope
        hold = stop_hold(view.upto(e.seq - 1), run=run, agent=agent)
        if hold is not None:
            found.append(HeldStart(e.seq, e.type, e.run, hold.reason))
    return tuple(found)


def _start_scope(e: Event, runs: RunsState) -> tuple[str | None, str | None] | None:
    """The (run, agent) an automatic start ``e`` needs free of holds, or None if it is none."""
    p = e.payload
    if e.type == "run.started" and p.get("cause") != PERSON_CAUSE:
        return None, str(p.get("agent"))
    if e.type == "stop.decided" and p.get("verdict") == "continue":
        if p.get("cause") not in AUTOMATIC_CAUSES or e.run is None:
            return None
        summary = runs.get(e.run)
        return e.run, None if summary is None else summary.agent
    if e.type == "step.started" and e.run is not None:
        return e.run, None
    return None
