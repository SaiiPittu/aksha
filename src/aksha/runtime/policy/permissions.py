"""What each declared value allows: the agent's list, a Task's list, the ceiling, the route.

These read the agent definition only, never the event store, so no fact written during a
run can widen them. Deny by default (D2): an agent with no tool list holds only the
implicit tools, ``finish`` and ``ask``.
"""

from __future__ import annotations

from collections.abc import Sequence

from aksha.runtime.definitions.types import Agent
from aksha.runtime.policy.types import ArgumentRule, Permissions, Route
from aksha.runtime.tasks.types import Task
from aksha.runtime.tools.loader_added import IMPLICIT_TOOLS
from aksha.runtime.tools.types import ToolDecl, ToolSpec


def agent_tools(agent: Agent) -> frozenset[str]:
    """The tools ``agent`` lists (own by local name, others by full name) plus the implicit ones."""
    listed = {t.spec.name if isinstance(t, ToolDecl) else t.tool for t in agent.tools}
    return frozenset(listed) | IMPLICIT_TOOLS


def task_tools(task: Task) -> frozenset[str]:
    """The tools ``task`` lists, plus ``finish``, plus ``ask`` when it declares an interrupt."""
    implicit = {"finish", "ask"} if task.interrupt is not None else {"finish"}
    return frozenset(task.tools) | implicit


def ceiling_of(agent: Agent, rules: Sequence[ArgumentRule] = ()) -> Permissions:
    """The widest run ceiling for ``agent``: its own list, with ``rules`` on it."""
    return Permissions(agent_tools(agent), tuple(rules))


def route_of(spec: ToolSpec) -> Route:
    """Where an allowed call of ``spec`` goes, by its effect class.

    An external tool with an approval rule raises an approval interrupt; one without
    writes an outbox entry first. Model tools run a routed model call; ``finish`` and
    ``ask`` are handled by the runtime. Read, store and exec tools are dispatched.
    """
    if spec.effect == "external":
        return "approval" if spec.approval is not None else "outbox"
    if spec.effect == "model":
        return "model"
    if spec.effect == "reserved":
        return "reserved"
    return "dispatch"
