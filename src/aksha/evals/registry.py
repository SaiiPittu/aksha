"""The agent-under-test registry: the hook every real agent registers through.

A registration names an agent as cases list it (``agents = ["research"]``), its model mode
and a factory that builds a fresh :class:`~aksha.evals.ports.AgentUnderTest` per episode.
The composition root (:func:`aksha.app.eval_agents`) fills the registry; nothing else does,
so which agents and adapters an eval can reach is decided in one place.

Each registration carries a definition hash, recorded on every result row (ADR-P22 D8): the
``definition_hash`` the agent's class declares (a runtime agent's adapter passes its agent
definition's hash), or else a hash of the source file that defines the adapter's class, so
a changed scripted agent changes its rows.

To register a real agent, write its adapter in ``aksha/evals/agents/<name>.py`` (a class
with ``name``, ``mode`` and ``run(episode)`` that drives the agent on the runtime with its
fake models, its source tools from :func:`aksha.evals.agents.tools.episode_tools`, and then
saves its artifacts into the episode in the bundle schema), and add
one ``registry.register(...)`` line in :func:`aksha.app.eval_agents`.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aksha.evals.errors import EvalError
from aksha.evals.ports import AgentUnderTest

if TYPE_CHECKING:
    from aksha.evals.case import Arm


@dataclass(frozen=True)
class Registration:
    """One registered agent."""

    name: str
    factory: Callable[[], AgentUnderTest]
    mode: str = "fake"
    description: str = ""
    definition_hash: str = ""
    definition_basis: str = ""
    armed: Callable[[Arm], AgentUnderTest] | None = None
    """Builds the agent for one arm of a comparison case; None when it runs no arms."""

    def build(self, arm: Arm | None = None) -> AgentUnderTest:
        """A fresh agent for one episode, under ``arm`` when the case has arms."""
        if arm is None:
            return self.factory()
        if self.armed is None:
            raise EvalError("arms_unsupported", f"agent {self.name!r} cannot run arm {arm.name}")
        return self.armed(arm)


class AgentRegistry:
    """Agents by name; a case's first listed agent is the one put through the episode."""

    def __init__(self) -> None:
        """An empty registry."""
        self._by_name: dict[str, Registration] = {}

    def register(
        self,
        name: str,
        factory: Callable[[], AgentUnderTest],
        *,
        mode: str = "fake",
        description: str = "",
        armed: Callable[[Arm], AgentUnderTest] | None = None,
    ) -> None:
        """Register ``name``; a second registration of one name is refused.

        ``armed`` builds the agent for one arm of a comparison case (its own re-entry
        setting and context policy); an agent registered without it runs no arms.
        """
        if name in self._by_name:
            raise EvalError("agent_registered", f"agent {name!r} is registered twice")
        digest, basis = definition_hash(factory)
        self._by_name[name] = Registration(name, factory, mode, description, digest, basis,
                                           armed)  # fmt: skip

    def get(self, name: str) -> Registration:
        """The registration of ``name``, or ``agent_not_registered``."""
        try:
            return self._by_name[name]
        except KeyError:
            raise EvalError(
                "agent_not_registered",
                f"no agent {name!r} is registered for evals; registered: {self.names()}",
            ) from None

    def has(self, name: str) -> bool:
        """Whether ``name`` is registered."""
        return name in self._by_name

    def names(self) -> tuple[str, ...]:
        """Every registered name, sorted."""
        return tuple(sorted(self._by_name))


def definition_hash(factory: Callable[[], AgentUnderTest]) -> tuple[str, str]:
    """The agent's definition hash and where it came from (``declared`` or ``source``).

    A factory (usually the adapter class) that declares a ``definition_hash`` string gives
    it; otherwise the hash is ``sha256:`` over the source file defining the factory, or
    ``unknown`` when that file cannot be read.
    """
    declared = getattr(factory, "definition_hash", None)
    if isinstance(declared, str) and declared:
        return declared, "declared"
    try:
        path = inspect.getsourcefile(factory)
        if path is None:
            return "unknown", "unknown"
        with open(path, "rb") as f:
            return "sha256:" + hashlib.sha256(f.read()).hexdigest(), "source"
    except (OSError, TypeError):
        return "unknown", "unknown"
