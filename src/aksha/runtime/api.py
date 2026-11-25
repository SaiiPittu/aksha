"""The extension API: the only runtime module agents and extensions import (ADR-P20 D2).

``API_VERSION`` is ``major.minor``. Adding a name bumps the minor version; removing or
changing one bumps the major. ``tests/architecture/api_surface.json`` pins every name and
signature below, so a change here is always a deliberate version bump.

What an agent or extension declares, grouped by the primitive that owns it:

1. Definitions (P20): ``Agent``, ``Extension``, ``Projection``.
2. Events and projections (P01): ``Event``, ``NewEvent``, ``EventTypeSpec``, ``Reducer``,
   ``LogView``, ``ArtifactsView``. An extension's own event types are declared data,
   ``Extension.event_types``; the loader registers them, so there is no register call.
3. Tasks (P15): ``Task``, ``Read``, ``KeySource``, ``source_keys``, ``artifact_keys``,
   ``FromField``, ``from_field``, ``Check``, ``CheckResult``.
4. Artifacts (P04): ``ArtifactKind``, ``RelationKind``, ``SaveRequest``, and what a save
   names: ``ArtifactRef``, ``InputRef``, ``RelationRequest``, ``Excerpt``, ``SELF``.
5. Tools (P10): ``ToolSpec``, ``ToolDecl``, ``ToolUse``, ``ToolBody``, ``RetryPolicy``,
   ``EffectClass``; what a body sees and returns: ``ToolContext``, ``ToolOutcome``, ``Ok``,
   ``Err``. A tool spec's parts owned elsewhere: ``EffectSpec`` (P03), ``ApprovalRule``
   (P17), ``SandboxNeeds`` and ``SandboxPolicy`` (P12).
6. Stop policies (P16): ``StopPolicyOptIn``, ``Goal``, ``Plan``, ``ForcedTool``,
   ``TodoReminder``. Interrupts (P17): ``DeclaredInterrupt``.
7. Models (P07): ``ModelAlias``, ``ModelClass``, ``ModelAdapterSpec``.
8. Limits and context (P14, P08): ``Budget``, ``Limits``, ``Backoff``, ``ContextPolicy``,
   ``SummaryPolicy``.
9. Grounding (P13): ``SpanDraft`` (what a read tool's ``Ok`` asks the runtime to mint),
   ``CitePolicy`` (an artifact kind's per-field cite rules), ``Unknown`` and
   ``maybe_unknown`` (an explicit unknown and the schema of a field that may hold one).
10. Other contributions: ``VersionedSourceSpec`` (P05), ``SandboxBackendSpec`` and
   ``EnvironmentSpec`` (P12, P28), ``SystemOfRecordSpec`` (P03), ``EgressPolicy`` (P26).
"""

from __future__ import annotations

from aksha.runtime.artifacts.types import (
    SELF,
    ArtifactKind,
    ArtifactRef,
    Excerpt,
    InputRef,
    RelationKind,
    RelationRequest,
    SaveRequest,
)
from aksha.runtime.budgets.types import Backoff, Budget, Limits
from aksha.runtime.context.types import ContextPolicy, SummaryPolicy
from aksha.runtime.definitions.types import Agent, Extension, Projection
from aksha.runtime.definitions.versions import API_VERSION
from aksha.runtime.egress.types import EgressPolicy
from aksha.runtime.events.catalogue import EventTypeSpec
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.grounding.types import CitePolicy, SpanDraft, Unknown, maybe_unknown
from aksha.runtime.interrupts.types import ApprovalRule, DeclaredInterrupt
from aksha.runtime.invalidation.types import VersionedSourceSpec
from aksha.runtime.models.types import ModelAdapterSpec, ModelAlias, ModelClass
from aksha.runtime.outbox.types import EffectSpec, SystemOfRecordSpec
from aksha.runtime.projections.ports import Reducer
from aksha.runtime.projections.view import ArtifactsView, LogView
from aksha.runtime.sandbox.types import (
    EnvironmentSpec,
    SandboxBackendSpec,
    SandboxNeeds,
    SandboxPolicy,
)
from aksha.runtime.stop_policy.types import ForcedTool, Goal, Plan, StopPolicyOptIn, TodoReminder
from aksha.runtime.tasks.types import (
    Check,
    CheckResult,
    FromField,
    KeySource,
    Read,
    Task,
    artifact_keys,
    from_field,
    source_keys,
)
from aksha.runtime.tools.types import (
    EffectClass,
    Err,
    Ok,
    RetryPolicy,
    ToolBody,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
    ToolUse,
)

__all__ = [
    "API_VERSION",
    "SELF",
    "Agent",
    "ApprovalRule",
    "ArtifactKind",
    "ArtifactRef",
    "ArtifactsView",
    "Backoff",
    "Budget",
    "Check",
    "CheckResult",
    "CitePolicy",
    "ContextPolicy",
    "DeclaredInterrupt",
    "EffectClass",
    "EffectSpec",
    "EgressPolicy",
    "EnvironmentSpec",
    "Err",
    "Event",
    "EventTypeSpec",
    "Excerpt",
    "Extension",
    "ForcedTool",
    "FromField",
    "Goal",
    "InputRef",
    "KeySource",
    "Limits",
    "LogView",
    "ModelAdapterSpec",
    "ModelAlias",
    "ModelClass",
    "NewEvent",
    "Ok",
    "Plan",
    "Projection",
    "Read",
    "Reducer",
    "RelationKind",
    "RelationRequest",
    "RetryPolicy",
    "SandboxBackendSpec",
    "SandboxNeeds",
    "SandboxPolicy",
    "SaveRequest",
    "SpanDraft",
    "StopPolicyOptIn",
    "SummaryPolicy",
    "SystemOfRecordSpec",
    "Task",
    "TodoReminder",
    "ToolBody",
    "ToolContext",
    "ToolDecl",
    "ToolOutcome",
    "ToolSpec",
    "ToolUse",
    "Unknown",
    "VersionedSourceSpec",
    "artifact_keys",
    "from_field",
    "maybe_unknown",
    "source_keys",
]
