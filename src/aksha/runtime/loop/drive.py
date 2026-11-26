"""``drive``: alternate stop-policy evaluation and ``run_turn`` until a run holds or ends.

ADR-P06 D1, D4 (Accepted: the loop never decides whether the work is over). The driver
holds no state between iterations; each pass re-reads the event store:

1. :func:`control_point` says where the run stands, from the log alone: ended, waiting on
   a person (``awaiting_person``: nothing new since a run-level HOLD, so nothing is
   written), a turn a CONTINUE authorised whose end is not recorded yet (run it, which
   returns every recorded step and runs only the rest: a resume mid-turn), or a stop
   candidate (the run's start, a recorded turn end no decision has judged, or ``next``
   after a HOLD).
2. Every candidate goes to stop-policy evaluation (ADR-P16), which commits one
   ``stop.decided`` and returns the verdict. Only CONTINUE starts a turn, the turn it
   charged (or, for a CONTINUE that re-authorises a turn a crash swallowed, the turn in
   progress). A HOLD of one instance offers ``next``, so the run's other work goes on
   (ADR-P16 S1); a HOLD of the run returns ``held``; a STOP returns ``ended``. PASS, PUSH,
   DONE and FAIL resolve inside evaluation and never reach the driver.

The turn limit, the stop-veto limit, a person's stop request and an open interrupt are all
read in evaluation, never here. :class:`LoopDriver` is the ``RunDriver`` recovery calls
to resume a run (ADR-P02 D10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from aksha.runtime.budgets.limits import turns_used
from aksha.runtime.events.envelope import Event
from aksha.runtime.execution.ports import DurableStepExecutor
from aksha.runtime.loop.classify import recorded_turn_end
from aksha.runtime.loop.turn import run_turn, stop_view
from aksha.runtime.loop.types import DriveResult, LoopSetup, TurnEnd
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS, RunSummary
from aksha.runtime.projections.view import LogView
from aksha.runtime.stop_policy.commit import commit_stop_decision
from aksha.runtime.stop_policy.errors import StopRefused
from aksha.runtime.stop_policy.evaluation import awaiting_person, evaluate_stop
from aksha.runtime.stop_policy.types import StopCandidate
from aksha.runtime.tasks.status import STOP_DECIDED
from aksha.runtime.tasks.types import InstanceRef

RESUME_CAUSE = "resume"
"""The cause of a resume record (ADR-P02 D7): it re-authorises a turn, it decides nothing."""

DEFAULT_MAX_EVALUATIONS = 10_000
"""More evaluations than any run needs: a bound on a runtime bug, never a run limit."""

PointState = Literal["ended", "held", "turn", "candidate"]


@dataclass(frozen=True)
class ControlPoint:
    """Where a run stands, read from the log: what the driver does next (module doc)."""

    state: PointState
    candidate: StopCandidate | None = None
    instance: InstanceRef | None = None
    turn: int | None = None


def _last_decision(view: LogView, run: str) -> Event | None:
    """The run's last ``stop.decided`` that decided something (resume records aside)."""
    last: Event | None = None
    for e in view.events:
        if e.run == run and e.type == STOP_DECIDED and e.payload.get("cause") != RESUME_CAUSE:
            last = e
    return last


def _summary(view: LogView, run: str) -> RunSummary:
    summary = rebuild(RUNS, view).get(run)
    if summary is None:
        raise StopRefused("run_not_open", f"run {run} never started")
    return summary


def control_point(view: LogView, run: str) -> ControlPoint:
    """Where ``run`` stands in ``view`` (module doc, point 1). Pure."""
    if _summary(view, run).outcome is not None:
        return ControlPoint("ended")
    if awaiting_person(view, run):
        return ControlPoint("held")
    last = _last_decision(view, run)
    nxt = ControlPoint("candidate", StopCandidate(run, "next"))
    if last is None or last.payload.get("verdict") != "continue" or last.instance is None:
        return nxt
    ref = InstanceRef.parse(run, last.instance)
    turn = int(last.payload.get("turn") or max(1, turns_used(view, run, last.instance)))
    end = recorded_turn_end(view, ref, turn)
    if end is None:
        return ControlPoint("turn", instance=ref, turn=turn)
    return ControlPoint("candidate", end.candidate())


def drive(
    setup: LoopSetup,
    executor: DurableStepExecutor,
    run: str,
    *,
    max_evaluations: int = DEFAULT_MAX_EVALUATIONS,
) -> DriveResult:
    """Drive ``run`` until a decision holds it for a person or ends it (module doc)."""
    ends: list[TurnEnd] = []
    point = control_point(setup.view(), run)
    for _ in range(max_evaluations):
        if point.state == "ended":
            outcome = _summary(setup.view(), run).outcome
            return DriveResult(run, "ended", outcome, tuple(ends))
        if point.state == "held":
            return DriveResult(run, "held", None, tuple(ends))
        if point.state == "turn":
            assert point.instance is not None and point.turn is not None
            end = run_turn(setup, executor, point.instance, point.turn)
            ends.append(end)
            candidate = end.candidate()
        else:
            assert point.candidate is not None
            candidate = point.candidate
        decision = evaluate_stop(stop_view(setup, run), candidate)
        commit_stop_decision(setup.store, setup.owner, decision)
        verdict, instance = decision.verdict, decision.instance
        if verdict.kind == "continue" and instance is not None:
            turn = decision.turn or max(1, turns_used(setup.view(), run, instance.instance))
            point = ControlPoint("turn", instance=instance, turn=turn)
        elif verdict.kind == "hold" and instance is not None:
            point = ControlPoint("candidate", StopCandidate(run, "next"))
        elif verdict.kind == "hold":
            point = ControlPoint("held")
        else:
            point = ControlPoint("ended")
    raise StopRefused("evaluation_loop", f"run {run} did not hold or end")


class LoopDriver:
    """The :class:`~aksha.runtime.execution.ports.RunDriver` that runs the agent loop.

    Recovery calls it with a fresh executor to resume a run (ADR-P02 D10); every recorded
    step returns its recorded result, so the run continues at its first unrecorded step.
    """

    def __init__(self, setup: LoopSetup) -> None:
        """Drive runs of ``setup``'s owner and agent."""
        self.setup = setup

    def __call__(self, run: RunSummary, steps: DurableStepExecutor, /) -> None:
        """Drive ``run`` until it holds or ends."""
        drive(self.setup, steps, run.run)
