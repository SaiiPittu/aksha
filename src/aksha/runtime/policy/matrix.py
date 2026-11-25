"""The permission matrix: one row per (agent, Task, tool), allowed or the refusal code and layer.

It is generated from the agent definitions through the gate's own :func:`permitted`
(layers 1 to 4, the ones that need no arguments), so the printed matrix and the gate
cannot disagree. Every tool in an agent's registry appears on every Task, loader-added
tools included.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from aksha.runtime.definitions.tree_checks import walk_tasks
from aksha.runtime.definitions.types import Agent
from aksha.runtime.policy.gate import permitted
from aksha.runtime.policy.permissions import ceiling_of, route_of
from aksha.runtime.policy.types import Permissions, Route, StopPolicyNarrowing
from aksha.runtime.tools.registry import ToolRegistry


@dataclass(frozen=True)
class MatrixRow:
    """One cell: allowed with its route, or refused with its code and layer."""

    agent: str
    task: str
    tool: str
    allowed: bool
    route: Route | None
    code: str | None
    layer: int | None

    @property
    def decision(self) -> str:
        """The cell as text: ``allowed (dispatch)`` or ``refused: not_in_task (layer 3)``."""
        if self.allowed:
            return f"allowed ({self.route})"
        return f"refused: {self.code} (layer {self.layer})"


@dataclass(frozen=True)
class PermissionMatrix:
    """Every cell, in agent, Task-walk and tool-name order."""

    rows: tuple[MatrixRow, ...]

    def cell(self, agent: str, task: str, tool: str) -> MatrixRow:
        """The row for ``(agent, task, tool)``; raises ``KeyError`` when there is none."""
        for row in self.rows:
            if (row.agent, row.task, row.tool) == (agent, task, tool):
                return row
        raise KeyError((agent, task, tool))

    def markdown(self) -> str:
        """The matrix as a Markdown table, for the README."""
        head = ["| Agent | Task | Tool | Decision |", "|---|---|---|---|"]
        body = [f"| {r.agent} | {r.task} | {r.tool} | {r.decision} |" for r in self.rows]
        return "\n".join(head + body) + "\n"


def matrix(
    entries: Sequence[tuple[Agent, ToolRegistry]],
    ceilings: Mapping[str, Permissions] | None = None,
    narrowing: StopPolicyNarrowing | None = None,
) -> PermissionMatrix:
    """The matrix of each ``(agent, registry)``. A ceiling defaults to the agent's own list."""
    chosen = ceilings or {}
    narrowing = narrowing or StopPolicyNarrowing()
    return PermissionMatrix(
        tuple(
            row
            for agent, registry in entries
            for row in _rows(agent, registry, chosen.get(agent.name, ceiling_of(agent)), narrowing)
        )
    )


def _rows(
    agent: Agent, registry: ToolRegistry, ceiling: Permissions, narrowing: StopPolicyNarrowing
) -> Iterator[MatrixRow]:
    """One agent's cells, Task by Task in walk order, tool by tool in name order."""
    for path, task in walk_tasks(agent.tasks):
        for tool in registry.names():
            refused = permitted(tool, agent, task, ceiling, narrowing, registry)
            if refused is None:
                route = route_of(registry.get(tool).spec)
                yield MatrixRow(agent.name, path, tool, True, route, None, None)
            else:
                yield MatrixRow(agent.name, path, tool, False, None, refused.code, refused.layer)
