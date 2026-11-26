"""``memory_extension`` and ``MemoryExtension.apply`` (ADR-P09 D1, D3, D10).

``apply`` is pure: it takes an agent definition and returns a new one, or raises
``MemoryRefused``. It names the ``memory`` extension, adds the three memory tools as the
agent's own tools (each closed over the spec, so the registry hash covers the caps), and,
in each intake Task: the tools, the memory kinds the spec writes, and a forced first call
of ``recall`` that holds until the instance has recalled once. ``recall`` is exempt from
observation masking (ADR-P08), so the hits stay in the intake context. Memory kinds in any
other Task's writes are refused, so is a memory tool in any other Task's tools, and so is a
fan-out intake Task: fan-out instances never recall and never write memory (D3).

An agent that declares no ``MemorySpec`` never calls ``apply`` and hashes the same with the
extension installed (D10): the definition hash covers only the extensions an agent names.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace

from aksha.runtime.api import (
    Agent,
    ArtifactKind,
    CheckResult,
    ContextPolicy,
    ForcedTool,
    Task,
    TaskView,
    ToolDecl,
    ToolUse,
)

from .kinds import KINDS
from .tools import memory_tools
from .types import MEMORY_KINDS, TOOL_NAMES, MemoryRefused, MemorySpec

NAME = "memory"
"""The extension's name and namespace."""


def not_recalled(view: TaskView) -> CheckResult:
    """True (force ``recall``) until this instance has completed one ``recall`` call."""
    return CheckResult(view.called("recall") == 0, "recall has not run in this instance")


@dataclass(frozen=True)
class MemoryExtension:
    """What turns memory on for one agent: the spec and the agent it was made for."""

    spec: MemorySpec
    agent: str

    kinds: tuple[ArtifactKind, ...] = KINDS

    def tools(self) -> tuple[ToolDecl, ...]:
        """The three tools, owned by the agent."""
        return memory_tools(self.spec, self.agent)

    def apply(self, defn: Agent) -> Agent:
        """``defn`` with memory on (module doc); ``MemoryRefused`` when it cannot be."""
        if defn.name != self.agent:
            raise MemoryRefused("agent_mismatch", f"made for {self.agent!r}, not {defn.name!r}")
        taken = sorted(set(TOOL_NAMES) & set(_tool_names(defn)))
        if taken:
            raise MemoryRefused("tool_name_taken", f"the agent already lists {taken[0]!r}")
        top = {t.id: t for t in defn.tasks}
        for intake in self.spec.intake_tasks:
            if intake not in top:
                raise MemoryRefused("intake_unknown", f"no top-level Task {intake!r}")
            if top[intake].per is not None:
                raise MemoryRefused("intake_fan_out", f"{intake!r} fans out; it cannot recall")
        for path, task in _walk(defn.tasks, ""):
            if path in self.spec.intake_tasks:
                continue
            written = sorted(set(task.writes) & set(MEMORY_KINDS))
            if written:
                raise MemoryRefused(
                    "memory_write_outside_intake", f"{path!r} writes {written[0]!r}"
                )
            listed = sorted(set(task.tools) & set(TOOL_NAMES))
            if listed:
                raise MemoryRefused("memory_tool_outside_intake", f"{path!r} lists {listed[0]!r}")
        tasks = tuple(self._intake(t) if t.id in self.spec.intake_tasks else t for t in defn.tasks)
        policy = defn.context_policy or ContextPolicy()
        policy = replace(policy, exempt_tools=policy.exempt_tools | {"recall"})
        extensions = defn.extensions if NAME in defn.extensions else (*defn.extensions, NAME)
        return replace(
            defn, tools=(*defn.tools, *self.tools()), tasks=tasks, context_policy=policy,
            extensions=extensions,
        )  # fmt: skip

    def _intake(self, task: Task) -> Task:
        writes = (*task.writes, *(k for k in MEMORY_KINDS if k in self.spec.write))
        return replace(
            task,
            tools=(*task.tools, *TOOL_NAMES),
            writes=writes,
            stop_policies=(ForcedTool("recall", when=not_recalled), *task.stop_policies),
        )


def memory_extension(spec: MemorySpec, agent: str) -> MemoryExtension:
    """Memory for ``agent`` under ``spec``; ``apply`` it to the agent's definition."""
    return MemoryExtension(spec, agent)


def _tool_names(defn: Agent) -> Iterator[str]:
    for t in defn.tools:
        yield t.tool if isinstance(t, ToolUse) else t.spec.name


def _walk(tasks: tuple[Task, ...], prefix: str) -> Iterator[tuple[str, Task]]:
    for t in tasks:
        path = f"{prefix}{t.id}"
        yield path, t
        yield from _walk(t.children, f"{path}/")
