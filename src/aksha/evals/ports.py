"""Protocols the eval runner needs from outside: the agent under test and its stores.

``AgentUnderTest`` is the seam the real agents plug into. An agent runs one episode through
the episode's tools (:class:`aksha.evals.episode.Episode`): it reads the world copy, saves
its outputs and relations, asks for approvals and sends effects. Everything it does is
recorded in the episode's own event store, and the grader reads only the bundle exported
from that store once the episode has ended. An agent that runs on the Aksha runtime keeps
its own runtime store and, at the end of its run, writes its saved artifacts into the
episode in the bundle schema (``docs/evals/README.md``); the adapter that does so is what
:mod:`aksha.app` registers.

``StoreOpener`` opens a fresh event store for one episode or one results folder; the
composition root supplies it, so the eval package never picks a backend.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from aksha.runtime.events.catalogue import Catalogue
from aksha.runtime.storage.ports import EventStore

if TYPE_CHECKING:
    from aksha.evals.bundle import EpisodeRecord
    from aksha.evals.case import EvalCase
    from aksha.evals.episode import Episode
    from aksha.evals.grade import CriterionResult, GradeContext


class AgentUnderTest(Protocol):
    """One agent the runner can put through an episode."""

    @property
    def name(self) -> str:
        """The agent's name as cases list it (``research``, ``patent``, ``scripted``)."""
        ...

    @property
    def mode(self) -> str:
        """The model mode it runs in: ``fake``, ``scripted``, ``replay`` or ``live``."""
        ...

    def run(self, episode: Episode) -> None:
        """Do the case's work through ``episode``'s tools; return when finished.

        It may be called again on the same episode after a restart (a change step with
        ``restart``), and must then continue from what the episode's store already holds.
        """
        ...


class StoreOpener(Protocol):
    """Opens a writer handle on a new or existing event store at ``path``."""

    def __call__(self, path: Path, catalogue: Catalogue) -> EventStore:
        """The writer handle; the caller closes it."""
        ...


class Grader(Protocol):
    """One criterion's check over an episode record and its world's key."""

    @property
    def id(self) -> str:
        """The criterion ID, such as ``research.competitor_count``."""
        ...

    @property
    def layer(self) -> str:
        """Its layer: ``gate``, ``invariant``, ``state``, ``citation``, ``trap`` or ``judge``."""
        ...

    @property
    def version(self) -> str:
        """Bumped on every change to the check; recorded on each result row."""
        ...

    def grade(self, rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> CriterionResult:
        """The criterion's result for this episode."""
        ...


class AgentFactory(Protocol):
    """Builds a fresh agent under test for one episode, given the case's agents list."""

    def __call__(self, agents: Sequence[str]) -> AgentUnderTest:
        """A new agent instance; one per episode so no state leaks between rollouts."""
        ...
