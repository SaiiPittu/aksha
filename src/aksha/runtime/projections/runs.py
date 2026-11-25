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
from aksha.runtime.projections.view import ArtifactsView


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
