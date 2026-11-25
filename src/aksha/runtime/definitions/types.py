"""Declared values: ``Agent``, ``Extension`` and ``Projection`` (ADR-P20 D3, D4, S1, S2).

An agent definition is one frozen ``Agent`` value; an extension is one frozen
``Extension`` value. Both are data the loader inspects. Neither can register a hook: the
only callables they carry are pure checks, reducers and tool bodies, which the runtime
calls through the interface that kind of thing already has (D3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aksha.runtime.artifacts.types import ArtifactKind, RelationKind
from aksha.runtime.budgets.types import Budget, Limits
from aksha.runtime.context.types import ContextPolicy
from aksha.runtime.egress.types import EgressPolicy
from aksha.runtime.events.catalogue import EventTypeSpec
from aksha.runtime.invalidation.types import VersionedSourceSpec
from aksha.runtime.models.types import ModelAdapterSpec, ModelAlias
from aksha.runtime.outbox.types import SystemOfRecordSpec
from aksha.runtime.projections.ports import Reducer
from aksha.runtime.sandbox.types import EnvironmentSpec, SandboxBackendSpec
from aksha.runtime.stop_policy.types import StopPolicyOptIn
from aksha.runtime.tasks.types import Check, Task
from aksha.runtime.tools.types import ToolDecl, ToolUse


@dataclass(frozen=True)
class Projection:
    """A read view an extension contributes, built by a reducer over the event store.

    ``name`` is local; the loader namespaces it (``ext.name``). ``reducer`` is an ADR-P01
    ``Reducer``, so it declares the event types it reads and is pure.
    """

    name: str
    reducer: Reducer[Any]

    @property
    def event_types(self) -> frozenset[str]:
        """The only event types the reducer is given."""
        return self.reducer.reads


@dataclass(frozen=True)
class Extension:
    """A package's declared contributions. ``version`` is informative; the hash uses file hashes.

    ``event_types`` are the extension's own event types (ADR-P01 D4). Each is named
    ``<extension>.<past participle>`` and names the extension as its writer; the loader
    registers them in the catalogue, and no core reducer reads them.
    """

    name: str
    version: str
    requires_api: str
    tools: tuple[ToolDecl, ...] = ()
    artifacts: tuple[ArtifactKind, ...] = ()
    relations: tuple[RelationKind, ...] = ()
    projections: tuple[Projection, ...] = ()
    tasks: tuple[Task, ...] = ()
    sandbox_backends: tuple[SandboxBackendSpec, ...] = ()
    model_adapters: tuple[ModelAdapterSpec, ...] = ()
    sources: tuple[VersionedSourceSpec, ...] = ()
    systems: tuple[SystemOfRecordSpec, ...] = ()
    environments: tuple[EnvironmentSpec, ...] = ()
    egress: tuple[EgressPolicy, ...] = ()
    event_types: tuple[EventTypeSpec, ...] = ()


@dataclass(frozen=True)
class Agent:
    """One agent definition: purpose, instructions, Task tree, tools, kinds and opt-ins.

    Names of kinds, relations, aliases and own tools are local; the loader prefixes
    ``name``. ``instructions`` and every ``Task.brief`` are paths of prompt files inside the
    agent folder, read and hashed by the loader. ``tools`` lists the agent's own tools as
    ``ToolDecl`` and names other tools (loader-added, ``ext.tool``) as ``ToolUse``.
    ``concurrency`` is how many Task instances the scheduler runs at once; v1 accepts only
    1 (ADR-P15 D5, ``concurrency_unsupported``).
    """

    name: str
    purpose: str
    instructions: str
    reads: tuple[str, ...]
    artifacts: tuple[ArtifactKind, ...]
    relations: tuple[RelationKind, ...] = ()
    tasks: tuple[Task, ...] = ()
    tools: tuple[ToolUse | ToolDecl, ...] = ()
    stop_policies: tuple[StopPolicyOptIn, ...] = ()
    completion: tuple[Check, ...] = ()
    model_aliases: tuple[ModelAlias, ...] = ()
    budget: Budget | None = None
    limits: Limits | None = None
    context_policy: ContextPolicy | None = None
    sources: tuple[VersionedSourceSpec, ...] = ()
    fakes: str = "fakes"
    phrases: str | None = "phrases.json"
    extensions: tuple[str, ...] = ()
    requires_api: str = ">=1.0,<2"
    concurrency: int = 1
