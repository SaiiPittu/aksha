"""Results as events: the suite's own results store and the projection over it.

Each suite run writes into ``<out>/results.sqlite``, an event store of its own (never the
store under test): ``eval.suite_started``, one ``eval.selftest_recorded`` per self-test
bundle, one ``eval.episode_graded`` per episode, one ``eval.relation_checked`` per base and
variant pair, then ``eval.suite_ended``. The report is a projection: :class:`SuiteResultsReducer`
folds those events, so ``aksha eval report <folder>`` rebuilds every number from the store,
and a grader fix that rescores saved bundles writes a new run, never an edit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from aksha.evals.events import results_catalogue
from aksha.evals.grade import CriterionResult
from aksha.evals.ports import StoreOpener
from aksha.runtime.canonical import thaw
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.view import ArtifactsView, load_view
from aksha.runtime.storage.ports import EventStore

RESULTS_FILE = "results.sqlite"


def owner_of(suite: str) -> str:
    """The results store's owner for one suite."""
    return f"eval:{suite}"


class ResultsWriter:
    """Appends one suite run's results to its results store."""

    def __init__(self, open_store: StoreOpener, out: Path, suite: str) -> None:
        """Open (creating) ``out/results.sqlite`` as the writer."""
        out.mkdir(parents=True, exist_ok=True)
        self.store: EventStore = open_store(out / RESULTS_FILE, results_catalogue())
        self.owner = owner_of(suite)

    def _add(self, boundary: str, type_: str, payload: Mapping[str, Any]) -> None:
        self.store.append(self.owner, boundary, [NewEvent(type_, dict(payload))])

    def suite_started(self, payload: Mapping[str, Any]) -> None:
        """``eval.suite_started``."""
        self._add("U1", "eval.suite_started", payload)

    def selftest(self, payload: Mapping[str, Any]) -> None:
        """``eval.selftest_recorded``."""
        self._add("U2", "eval.selftest_recorded", payload)

    def episode(self, payload: Mapping[str, Any]) -> None:
        """``eval.episode_graded``."""
        self._add("U2", "eval.episode_graded", payload)

    def relation(self, payload: Mapping[str, Any]) -> None:
        """``eval.relation_checked``."""
        self._add("U2", "eval.relation_checked", payload)

    def suite_ended(self, payload: Mapping[str, Any]) -> None:
        """``eval.suite_ended``."""
        self._add("U3", "eval.suite_ended", payload)

    def close(self) -> None:
        """Close the store."""
        self.store.close()


def criteria_json(results: Sequence[CriterionResult]) -> list[dict[str, object]]:
    """Criterion results as payload items."""
    return [r.as_json() for r in results]


@dataclass(frozen=True)
class SuiteResults:
    """The projection: everything one suite run recorded, in order."""

    started: Mapping[str, Any] | None = None
    selftests: tuple[Mapping[str, Any], ...] = ()
    episodes: tuple[Mapping[str, Any], ...] = ()
    relations: tuple[Mapping[str, Any], ...] = ()
    ended: Mapping[str, Any] | None = None


class SuiteResultsReducer:
    """Folds a results store's ``eval.*`` events into :class:`SuiteResults`. Pure."""

    name = "eval_suite_results"
    reads = frozenset(
        {
            "eval.suite_started",
            "eval.selftest_recorded",
            "eval.episode_graded",
            "eval.relation_checked",
            "eval.suite_ended",
        }
    )

    def initial(self, args: object) -> SuiteResults:
        """No results yet."""
        return SuiteResults()

    def apply(self, state: SuiteResults, event: Event, artifacts: ArtifactsView) -> SuiteResults:
        """The state after one event; a new suite start resets it (latest run wins)."""
        p = thaw(event.payload)
        if event.type == "eval.suite_started":
            return SuiteResults(started=p)
        if event.type == "eval.selftest_recorded":
            return replace(state, selftests=(*state.selftests, p))
        if event.type == "eval.episode_graded":
            return replace(state, episodes=(*state.episodes, p))
        if event.type == "eval.relation_checked":
            return replace(state, relations=(*state.relations, p))
        return replace(state, ended=p)


def project_results(store: EventStore, suite: str) -> SuiteResults:
    """The latest suite run in ``store``."""
    return rebuild(SuiteResultsReducer(), load_view(store, owner_of(suite)))
