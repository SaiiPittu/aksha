"""The agent-under-test registry: the hook every real agent registers through.

A registration names an agent as cases list it (``agents = ["research"]``), its model mode
and a factory that builds a fresh :class:`~aksha.evals.ports.AgentUnderTest` per episode.
The composition root (:func:`aksha.app.eval_agents`) fills the registry; nothing else does,
so which agents and adapters an eval can reach is decided in one place.

To register a real agent, write its adapter in ``aksha/evals/agents/<name>.py`` (a class
with ``name``, ``mode`` and ``run(episode)`` that drives the agent on the runtime with its
fake models and then saves its artifacts into the episode in the bundle schema), and add
one ``registry.register(...)`` line in :func:`aksha.app.eval_agents`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from aksha.evals.errors import EvalError
from aksha.evals.ports import AgentUnderTest


@dataclass(frozen=True)
class Registration:
    """One registered agent."""

    name: str
    factory: Callable[[], AgentUnderTest]
    mode: str = "fake"
    description: str = ""


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
    ) -> None:
        """Register ``name``; a second registration of one name is refused."""
        if name in self._by_name:
            raise EvalError("agent_registered", f"agent {name!r} is registered twice")
        self._by_name[name] = Registration(name, factory, mode, description)

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
