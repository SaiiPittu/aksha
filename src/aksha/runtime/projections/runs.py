"""The runs projection: every run of one owner, its agent and cause, and how it ended.

The first core reducer, and the model for the others: a frozen state, a pure ``apply``
that returns a new state, and a declared ``reads``. Recovery (ADR-P02) reads it to find
the runs a crash left open.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from aksha.runtime.events.envelope import Event
from aksha.runtime.projections.view import ArtifactsView, LogView


@dataclass(frozen=True)
class RunSummary:
    """One run: who ran it, why, its outcome (None while open) and its definition hash.

    ``definition_hash`` is what ``run.started`` recorded (ADR-P20 D7); recovery compares
    it with the loaded definition's and ends the run ``definition_changed`` if they differ.
    """

    run: str
    agent: str
    cause: str
    outcome: str | None = None
    definition_hash: str | None = None


RunsState = Mapping[str, RunSummary]
"""Run ID to summary; frozen."""


class Runs:
    """Reducer over ``run.started`` and ``run.ended``, keyed by run ID."""

    name = "runs"
    reads = frozenset({"run.started", "run.ended"})

    def initial(self, args: Hashable) -> RunsState:
        """No runs."""
        return MappingProxyType({})

    def apply(self, state: RunsState, event: Event, artifacts: ArtifactsView) -> RunsState:
        """Open a run on ``run.started``; record its outcome on ``run.ended``."""
        run = event.run or ""
        if event.type == "run.started":
            p = event.payload
            recorded = p.get("definition_hash")
            summary = RunSummary(
                run, str(p["agent"]), str(p["cause"]),
                definition_hash=None if recorded is None else str(recorded),
            )  # fmt: skip
        elif run in state:
            summary = replace(state[run], outcome=str(event.payload["outcome"]))
        else:
            return state  # an end with no start: nothing to record
        return MappingProxyType({**state, run: summary})


RUNS = Runs()
"""The one instance the runtime uses."""


def open_runs(state: RunsState) -> tuple[str, ...]:
    """The IDs of runs with no outcome yet, sorted."""
    return tuple(sorted(r for r, s in state.items() if s.outcome is None))


@dataclass(frozen=True)
class RunEnded:
    """One ended run as ``ended_runs`` reads it back (ADR-P09 D6, D11).

    ``completed`` lists the Task instances whose latest completion check in the run passed,
    in the order they first passed; ``outputs`` the artifact versions the run saved, in
    commit order; ``blockers`` what ``run.ended`` recorded.
    """

    run: str
    agent: str
    outcome: str
    reason: str
    blockers: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()


def ended_runs(view: LogView, agents: frozenset[str]) -> tuple[RunEnded, ...]:
    """Every ended run of ``agents`` in the owner's ``view``, in the order they ended.

    The tenant is the view's owner: a log view never holds another owner's events.
    """
    agent_of: dict[str, str] = {}
    checks: dict[str, dict[str, bool]] = {}
    outputs: dict[str, list[str]] = {}
    out: list[RunEnded] = []
    for e in view.events:
        run = e.run or ""
        if e.type == "run.started":
            agent_of[run] = str(e.payload.get("agent"))
        elif agent_of.get(run) not in agents:
            continue
        elif e.type == "completion_check.evaluated":
            key = e.payload.get("key")
            task = str(e.payload.get("task"))
            instance = task if key is None else _keyed(task, str(key))
            checks.setdefault(run, {})[instance] = bool(e.payload.get("ok"))
        elif e.type == "artifact.saved":
            outputs.setdefault(run, []).append(str(e.payload.get("ref")))
        elif e.type == "run.ended":
            p = e.payload
            out.append(
                RunEnded(
                    run=run,
                    agent=agent_of[run],
                    outcome=str(p["outcome"]),
                    reason=str(p.get("reason", "")),
                    blockers=tuple(str(b) for b in p.get("blockers", ())),
                    completed=tuple(i for i, ok in checks.get(run, {}).items() if ok),
                    outputs=tuple(outputs.get(run, ())),
                )
            )
    return tuple(out)


def _keyed(task: str, key: str) -> str:
    """The instance ID of ``task`` at ``key``: the key rides on the top-level segment."""
    head, sep, rest = task.partition("/")
    return f"{head}[{key}]{sep}{rest}"
