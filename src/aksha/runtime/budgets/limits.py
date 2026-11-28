"""The limits inside a run and the retry count, each a reducer over the event store.

ADR-P14 D3 and D7. Nothing here keeps a count between calls: every function reads a
view (or the events of one) and derives the number, so a restart never resets it.

==================  =======  ==================================================  ==============
Limit               Default  Counted from                                        Bites when
==================  =======  ==================================================  ==============
turn limit          10       turns authorised since the instance was entered     used >= limit
stop-veto limit     3        refused finishes since entry, this one included     used >= limit
restart limit       3        resume records since the run's last progress        used >= limit
automatic starts    3 / 5 m  automatic starts recorded inside the window          used >= count
timer starts        3 / 5 m  an agent's runs opened by a timer inside the window  used >= count
==================  =======  ==================================================  ==============

A run's ``run.started{turn_factor}`` multiplies its turn limits (:func:`turn_factor`).

"Bites" means the next turn, veto or resume would exceed the limit: the turn limit lets
ten turns run and fails the eleventh, the stop-veto limit gives the first two refused
finishes back to the model as the next turn's message and fails the instance on the third
(so a limit of 1 fails the first refusal and the check's message never reaches the model),
and the restart limit lets three resumes run and ends the run on the fourth (ADR-P02 D8:
"above 3 the run stops").

The turn limit, like the stop-veto limit, counts one entry's work: a rebuild re-entry
(ADR-P05 D7) writes a new ``task.entered`` and starts the count again. Counted over the
instance's whole life, a rebuild would inherit the turns of the version it replaces, and an
instance whose limit is sized to one build could never be rebuilt. Turn numbers still run
over the whole life (``turns_used``), so a step id is never reused.
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

    An attempt a crash interrupted left no fact until its step resumes; the executor then
    records it as ``step.failed{interrupted}`` before the next attempt (ADR-P14 B1), so it
    counts and the next attempt takes the number after it.
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


def turns_since_entry(view: LogView, run: str, instance: str) -> int:
    """Model turns authorised for ``instance`` of ``run`` since its last ``task.entered``.

    What the turn limit reads; a resumed turn adds none, as in ``turns_used``.
    """
    turns: set[tuple[str, ...]] = set()
    for e in view.events:
        if e.run != run or e.instance != instance:
            continue
        if e.type == records.TASK_ENTERED:
            turns.clear()
        elif key := records.charged_turn(e):
            turns.add(key)
    return len(turns)


def vetoes_since_entry(view: LogView, run: str, instance: str) -> int:
    """Completion-check vetoes of ``instance`` since its last entry or reset (ADR-P15 D2).

    A ``completion_check.reset`` (a person answered a blocked instance, or a rebuild run
    re-entered it) starts the count again, like a ``task.entered``.
    """
    count = 0
    for e in view.events:
        if e.run != run or e.instance != instance:
            continue
        if e.type in records.VETO_COUNT_RESETS:
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


def turn_factor(view: LogView, run: str) -> int:
    """The multiplier ``run``'s turn limits run under: ``run.started{turn_factor}``, else 1.

    Live mode records one (ADR-P14 amendment L1) because a real model needs more turns than
    a scripted fake; a run that records none runs under its definition's own limits. Read
    from the log, so a resumed run keeps the factor it started under.
    """
    for e in view.events:
        if e.type == records.RUN_STARTED and e.run == run:
            value = e.payload.get("turn_factor")
            return value if isinstance(value, int) and value >= 1 else 1
    return 1


def limit_reached(
    view: LogView, run: str, instance: str | None, limits: Limits
) -> tuple[LimitHit, ...]:
    """Every limit that bites before ``run`` (or its Task ``instance``) goes on. Pure.

    With ``instance`` None only the run's restart limit is read. The stop policy applies
    the hits to a proposed CONTINUE (ADR-P16 D3); a hit is a trail entry, not an event.
    The stop-veto count is that of a finish about to be refused: the recorded vetoes plus
    this one, as ``TaskPolicy.limit`` counts it against ``Task.max_finish_vetoes``.
    """
    hits: list[LimitHit] = []
    if instance is not None:
        turns = turns_since_entry(view, run, instance)
        allowed = limits.turn_limit * turn_factor(view, run)
        if turns >= allowed:
            hits.append(LimitHit("turn_limit", turns, allowed, instance))
        vetoes = vetoes_since_entry(view, run, instance) + 1  # the refusal being judged
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


def timer_starts(view: LogView, agent: str, now: datetime, window_ms: int) -> int:
    """Runs of ``agent`` a timer opened (``run.started{cause: timer}``) in the window.

    ADR-P25 D2: a reminder's run has no run of its own to count against before it opens,
    so its automatic-start limit counts the agent's timer-opened runs; a reminder that
    schedules itself again is capped at ``Limits.auto_starts`` per window.
    """
    since = now - timedelta(milliseconds=window_ms)
    return sum(
        1
        for e in view.events
        if e.type == "run.started"
        and e.payload.get("cause") == records.TIMER_CAUSE
        and e.payload.get("agent") == agent
        and since < datetime.fromisoformat(e.at) <= now
    )
