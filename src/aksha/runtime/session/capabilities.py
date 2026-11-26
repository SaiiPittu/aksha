"""The capability record: what this runtime, as configured, can do (ADR-P19 D7).

It is derived from the session's configuration and the loaded definitions on every call
and never stored. A client branches on it, never on an agent's or a backend's name: an
interface hides a button whose command is absent. The honesty rule holds by construction
for commands: ``commands`` is :meth:`Session.commands`, the same set ``dispatch`` checks
before it carries a command out, and a test dispatches every listed command.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from aksha.runtime.definitions.versions import API_VERSION
from aksha.runtime.session.service import Session, SessionAgent
from aksha.runtime.session.streams import STREAM_SCHEMA_VERSION


@dataclass(frozen=True)
class StreamCapabilities:
    """What the event stream sends (D5, D6)."""

    schema_version: int
    synchronized_marker: bool
    resync: bool
    transient: bool
    max_replay: int | None


@dataclass(frozen=True)
class AgentCapabilities:
    """One loaded agent as a client may show it."""

    name: str
    purpose: str
    definition_hash: str
    interrupt_labels: tuple[tuple[str, str], ...]
    effectful_tools: tuple[str, ...]
    approval_tools: tuple[str, ...]
    stop_policies: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityRecord:
    """The runtime's capability record (D7)."""

    api_version: str
    runtime_commit: str
    dirty: bool
    mode: str
    commands: tuple[str, ...]
    interrupt_kinds: tuple[str, ...]
    model_classes: Mapping[str, str]
    sandbox_backends: tuple[str, ...]
    headless: bool
    router: bool
    stream: StreamCapabilities
    agents: tuple[AgentCapabilities, ...]

    def as_json(self) -> dict[str, Any]:
        """The record as JSON data."""
        out = asdict(self)
        out["model_classes"] = dict(self.model_classes)
        loaded: dict[str, Any] = json.loads(json.dumps(out))  # tuples become lists
        return loaded


def _agent(a: SessionAgent) -> AgentCapabilities:
    labels: set[tuple[str, str]] = set()
    policies: set[str] = {type(p).__name__ for p in a.agent.stop_policies}
    for node in a.tree.nodes.values():
        task = node.task
        if task.interrupt is not None:
            labels.add((task.interrupt.kind, task.interrupt.label))
        policies |= {type(p).__name__ for p in task.stop_policies}
    effectful = tuple(
        sorted(n for n in a.registry.names() if a.registry.get(n).spec.external is not None)
    )
    return AgentCapabilities(
        name=a.name,
        purpose=a.agent.purpose,
        definition_hash=a.definition_hash,
        interrupt_labels=tuple(sorted(labels)),
        effectful_tools=effectful,
        approval_tools=tuple(sorted(a.approval_specs())),
        stop_policies=tuple(sorted(policies)),
    )


def capabilities(
    session: Session,
    *,
    runtime_commit: str = "unknown",
    dirty: bool = False,
    sandbox_backends: tuple[str, ...] = (),
    max_replay: int | None = None,
) -> CapabilityRecord:
    """The record of ``session`` as configured now (module doc)."""
    agents = tuple(_agent(a) for a in sorted(session.agents.values(), key=lambda a: a.name))
    modes = sorted({a.table.mode for a in session.agents.values()})
    kinds = {"input", "choice", "approval", "budget", "review"}
    if session.router is not None:
        kinds.add("route")
    classes: dict[str, str] = {}
    for a in sorted(session.agents.values(), key=lambda a: a.name):
        for b in a.table.bindings():
            classes[f"{a.name}/{b.alias}"] = f"{b.model_class}:{b.adapter}"
    available = session.sandbox()
    return CapabilityRecord(
        api_version=API_VERSION,
        runtime_commit=runtime_commit,
        dirty=dirty,
        mode=modes[0] if len(modes) == 1 else ",".join(modes) or "none",
        commands=tuple(sorted(session.commands())),
        interrupt_kinds=tuple(sorted(kinds)),
        model_classes=classes,
        sandbox_backends=sandbox_backends,
        headless=available is not None and available.available,
        router=session.router is not None,
        stream=StreamCapabilities(STREAM_SCHEMA_VERSION, True, True, False, max_replay),
        agents=agents,
    )
