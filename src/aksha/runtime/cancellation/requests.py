"""Stop requests read from the log (ADR-P18 D1 to D3, D5). Every function is pure.

A request is honoured by the ``stop.decided`` that ended its run with reason
``stop_requested``. A run-scope request is pending while its run is open and no such
decision follows it; a run with a pending request must end at its next stop candidate,
and no new step of it may start.
"""

from __future__ import annotations

from aksha.runtime.cancellation.types import STOP_REQUEST, STOP_REQUESTED, StopRequestView
from aksha.runtime.events.envelope import Event
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS, open_runs
from aksha.runtime.projections.view import LogView

STOP_DECIDED = "stop.decided"

STOP_VIEW_TYPES = frozenset({"run.started", "run.ended", STOP_REQUEST, STOP_DECIDED})
"""The event types these functions read; a filtered view of them is enough."""


def _honours(e: Event, run: str) -> bool:
    p = e.payload
    return (
        e.type == STOP_DECIDED
        and e.run == run
        and p.get("verdict") == "stop"
        and p.get("reason") == STOP_REQUESTED
    )


def stop_requests(view: LogView, run: str) -> tuple[StopRequestView, ...]:
    """Every stop request of ``run``, in order, each with the decision that honoured it."""
    found: list[StopRequestView] = []
    honoured: int | None = None
    for e in view.events:
        if e.run != run:
            continue
        if e.type == STOP_REQUEST:
            p = e.payload
            found.append(
                StopRequestView(
                    run=run, scope=p["scope"], reason=str(p["reason"]), by=str(p["by"]),
                    command_id=str(p["command_id"]), seq=e.seq,
                )
            )  # fmt: skip
        elif honoured is None and _honours(e, run):
            honoured = e.seq
    if honoured is None:
        return tuple(found)
    return tuple(
        StopRequestView(r.run, r.scope, r.reason, r.by, r.command_id, r.seq,
                        honoured if r.seq < honoured else None)
        for r in found
    )  # fmt: skip


def pending_stop(view: LogView, run: str) -> StopRequestView | None:
    """The newest request of open ``run`` not yet honoured, or None."""
    summary = rebuild(RUNS, view).get(run)
    if summary is None or summary.outcome is not None:
        return None
    pending = [r for r in stop_requests(view, run) if r.honoured_by is None]
    return pending[-1] if pending else None


def runs_to_resume_for_stop(view: LogView) -> tuple[str, ...]:
    """D3: open runs with a pending stop; each gets one resume candidate, even if held."""
    return tuple(r for r in open_runs(rebuild(RUNS, view)) if pending_stop(view, r) is not None)


def stop_before_step(view: LogView, run: str) -> bool:
    """D5: whether a new step of ``run`` must be skipped because a stop is pending."""
    return pending_stop(view, run) is not None
