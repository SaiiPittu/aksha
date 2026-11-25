"""The limits inside a run and the retry count, each a reducer over the event store.

ADR-P14 D3 and D7. Nothing here keeps a count between calls: every function reads a
view (or the events of one) and derives the number, so a restart never resets it.

==================  =======  ==================================================  ==============
Limit               Default  Counted from                                        Bites when
==================  =======  ==================================================  ==============
turn limit          10       turns authorised for the Task instance              used >= limit
stop-veto limit     3        vetoes since the instance was last entered          used >= limit
restart limit       3        resume records since the run's last progress        used >= limit
automatic starts    3 / 5 m  automatic starts recorded inside the window          used >= count
==================  =======  ==================================================  ==============

"Bites" means the next turn, veto or resume would exceed the limit: the turn limit lets
ten turns run and fails the eleventh, and the restart limit lets three resumes run and
ends the run on the fourth (ADR-P02 D8: "above 3 the run stops").
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from aksha.runtime.budgets import records
from aksha.runtime.budgets.types import LimitHit, Limits
from aksha.runtime.events.envelope import Event
from aksha.runtime.projections.view import LogView

ATTEMPT_TYPES = frozenset({"step.failed", "attempt.discarded"})
"""The facts an attempt leaves: a failure, or the late value of an abandoned attempt."""


def next_attempt(events: Iterable[Event], step_id: str) -> int:
    """One more than the highest attempt ``events`` record for ``step_id`` (from 1). Pure.

    An attempt that crashed unrecorded left no fact, so it runs again under its own number.
    """
    numbers = [
        int(e.payload["attempt"]) for e in events if e.step == step_id and e.type in ATTEMPT_TYPES
    ]
    return max(numbers, default=0) + 1


def attempt_number(view: LogView, step_id: str) -> int:
    """The number of the step's next attempt, from the event store (ADR-P14 D3)."""
    return next_attempt(view.events, step_id)


def turns_used(view: LogView, run: str, instance: str) -> int:
    """Model turns authorised for Task ``instance`` of ``run``; a resumed turn adds none."""
    turns = {
        key
        for e in view.events
        if e.run == run and e.instance == instance and (key := records.charged_turn(e))
    }
    return len(turns)


def vetoes_since_entry(view: LogView, run: str, instance: str) -> int:
    """Completion-check vetoes of ``instance`` since its last ``task.entered``."""
    count = 0
    for e in view.events:
        if e.run != run or e.instance != instance:
            continue
        if e.type == records.TASK_ENTERED:
            count = 0
        elif records.is_veto(e):
            count += 1
    return count


def resumes_since_progress(view: LogView, run: str) -> int:
    """Resume records of ``run`` since its last completed tool step or saved artifact."""
    count = 0
    for e in view.events:
        if e.run != run:
            continue
        if records.is_progress(e):
            count = 0
        elif records.is_resume_record(e):
            count += 1
    return count


def limit_reached(
    view: LogView, run: str, instance: str | None, limits: Limits
) -> tuple[LimitHit, ...]:
    """Every limit that bites before ``run`` (or its Task ``instance``) goes on. Pure.

    With ``instance`` None only the run's restart limit is read. The stop policy applies
    the hits to a proposed CONTINUE (ADR-P16 D3); a hit is a trail entry, not an event.
    """
    hits: list[LimitHit] = []
    if instance is not None:
        turns = turns_used(view, run, instance)
        if turns >= limits.turn_limit:
            hits.append(LimitHit("turn_limit", turns, limits.turn_limit, instance))
        vetoes = vetoes_since_entry(view, run, instance)
        if vetoes >= limits.stop_veto_limit:
            hits.append(LimitHit("stop_veto_limit", vetoes, limits.stop_veto_limit, instance))
    resumes = resumes_since_progress(view, run)
    if resumes >= limits.restart_limit:
        hits.append(LimitHit("restart_limit", resumes, limits.restart_limit))
    return tuple(hits)


def automatic_starts(view: LogView, run: str, now: datetime, window_ms: int) -> int:
    """Automatic starts of ``run`` recorded in the ``window_ms`` before ``now``.

    The window reads each event's recorded ``at``, never a clock: ``now`` is the caller's
    injected clock reading, so a fake run counts the same every time.
    """
    since = now - timedelta(milliseconds=window_ms)
    return sum(
        1
        for e in view.events
        if e.run == run
        and records.is_automatic_start(e)
        and since < datetime.fromisoformat(e.at) <= now
    )


def auto_start_allowed(view: LogView, run: str, limits: Limits, now: datetime) -> bool:
    """Whether one more automatic start of ``run`` fits ``limits.auto_starts`` (designed)."""
    count, window_ms = limits.auto_starts
    return automatic_starts(view, run, now, window_ms) < count
