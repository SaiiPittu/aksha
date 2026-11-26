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
   ``FromField``, ``from_field``, ``Check``, ``CheckResult``; what a completion check
   reads: ``TaskView``, ``InstanceRef``, ``InstanceStatus``.
4. Artifacts (P04): ``ArtifactKind``, ``RelationKind``, ``SaveRequest``, and what a save
   names: ``ArtifactRef``, ``InputRef``, ``RelationRequest``, ``Excerpt``, ``SELF``.
5. Tools (P10): ``ToolSpec``, ``ToolDecl``, ``ToolUse``, ``ToolBody``, ``RetryPolicy``,
   ``EffectClass``; what a body sees and returns: ``ToolContext``, ``ToolOutcome``, ``Ok``,
   ``Err``. A tool spec's parts owned elsewhere: ``EffectSpec`` (P03), ``ApprovalRule``
   (P17), ``SandboxNeeds``, ``SandboxPolicy``, ``ExecLimits`` and ``TreeManifest`` (P12).
   What a body's handles answer: ``ExecOutcome`` (``ToolContext.exec``, P12),
   ``HttpResponse`` and ``EgressRefused`` (``ToolContext.http``, P26).
6. Stop policies (P16): ``StopPolicyOptIn``, ``Goal``, ``Plan``, ``ForcedTool``,
   ``TodoReminder``. Interrupts (P17): ``DeclaredInterrupt``.
7. Models (P07): ``ModelAlias``, ``ModelClass``, ``ModelAdapterSpec``; what an alias
   declares: ``FieldSpec``, ``QuestionSpec``, ``ConfidenceThresholds``; what a rule-based
   fake adapter reads and returns: ``ModelCapabilities``, ``ExtractionRequest``,
   ``ExtractionResult``, ``FieldAnswer``, ``SpanRef``, ``ClassificationRequest``,
   ``ClassificationResult``, ``ClassAnswer``, ``GenerationRequest``, ``GenerationResult``,
   ``Message``, ``ToolCall``, ``Usage``; the bases ``RuleExtractionModel``,
   ``RuleClassificationModel``, ``RuleGenerationModel``; and the fixed-point helpers
   ``ppm``, ``distribution_ppm``, ``decimal_string``.
8. Limits and context (P14, P08): ``Budget``, ``Limits``, ``Backoff``, ``ContextPolicy``,
   ``SummaryPolicy``, ``ResumePolicy``.
9. Grounding (P13): ``SpanDraft`` (what a read tool's ``Ok`` asks the runtime to mint),
   ``RecordReader`` and ``ReturnedText`` (the reader a body reads records through, and
   what it returns),
   ``CitePolicy`` (an artifact kind's per-field cite rules), ``Unknown`` and
   ``maybe_unknown`` (an explicit unknown and the schema of a field that may hold one),
   ``where_part`` and ``part_version`` (a read of some rows of a table, versioned by
   exactly those rows, so a change to other rows moves nothing built on it).
10. Other contributions: ``VersionedSourceSpec`` (P05), ``SandboxBackendSpec`` and
   ``EnvironmentSpec`` (P12, P28), ``SystemOfRecordSpec`` (P03), ``EgressPolicy`` (P26);
   what a contributed system of record implements and answers: ``SystemOfRecord`` and
   ``SystemReceipt`` (P03).
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
from aksha.runtime.context.types import ContextPolicy, ResumePolicy, SummaryPolicy
from aksha.runtime.definitions.types import Agent, Extension, Projection
from aksha.runtime.definitions.versions import API_VERSION
from aksha.runtime.egress.errors import EgressRefused
from aksha.runtime.egress.ports import HttpResponse
from aksha.runtime.egress.types import EgressPolicy
from aksha.runtime.events.catalogue import EventTypeSpec
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.grounding.parts import part_version, where_part
from aksha.runtime.grounding.ports import RecordReader
from aksha.runtime.grounding.types import (
    CitePolicy,
    ReturnedText,
    SpanDraft,
    Unknown,
    maybe_unknown,
)
from aksha.runtime.interrupts.types import ApprovalRule, DeclaredInterrupt
from aksha.runtime.invalidation.types import VersionedSourceSpec
from aksha.runtime.models.fixed import decimal_string, distribution_ppm, ppm
from aksha.runtime.models.rules import (
    RuleClassificationModel,
    RuleExtractionModel,
    RuleGenerationModel,
)
from aksha.runtime.models.types import (
    ClassAnswer,
    ClassificationRequest,
    ClassificationResult,
    ConfidenceThresholds,
    ExtractionRequest,
    ExtractionResult,
    FieldAnswer,
    FieldSpec,
    GenerationRequest,
    GenerationResult,
    Message,
    ModelAdapterSpec,
    ModelAlias,
    ModelCapabilities,
    ModelClass,
    QuestionSpec,
    SpanRef,
    ToolCall,
    Usage,
)
from aksha.runtime.outbox.ports import SystemOfRecord
from aksha.runtime.outbox.types import EffectSpec, SystemOfRecordSpec, SystemReceipt
from aksha.runtime.projections.ports import Reducer
from aksha.runtime.projections.view import ArtifactsView, LogView
from aksha.runtime.sandbox.exec import ExecOutcome
from aksha.runtime.sandbox.types import (
    EnvironmentSpec,
    SandboxBackendSpec,
    SandboxNeeds,
    SandboxPolicy,
    TreeManifest,
)
from aksha.runtime.sandbox.types import Limits as ExecLimits
from aksha.runtime.stop_policy.types import ForcedTool, Goal, Plan, StopPolicyOptIn, TodoReminder
from aksha.runtime.tasks.types import (
    Check,
    CheckResult,
    FromField,
    InstanceRef,
    InstanceStatus,
    KeySource,
    Read,
    Task,
    artifact_keys,
    from_field,
    source_keys,
)
from aksha.runtime.tasks.view import TaskView
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
    "ClassAnswer",
    "ClassificationRequest",
    "ClassificationResult",
    "ConfidenceThresholds",
    "ContextPolicy",
    "DeclaredInterrupt",
    "EffectClass",
    "EffectSpec",
    "EgressPolicy",
    "EgressRefused",
    "EnvironmentSpec",
    "Err",
    "Event",
    "EventTypeSpec",
    "Excerpt",
    "ExecLimits",
    "ExecOutcome",
    "Extension",
    "ExtractionRequest",
    "ExtractionResult",
    "FieldAnswer",
    "FieldSpec",
    "ForcedTool",
    "FromField",
    "GenerationRequest",
    "GenerationResult",
    "Goal",
    "HttpResponse",
    "InputRef",
    "InstanceRef",
    "InstanceStatus",
    "KeySource",
    "Limits",
    "LogView",
    "Message",
    "ModelAdapterSpec",
    "ModelAlias",
    "ModelCapabilities",
    "ModelClass",
    "NewEvent",
    "Ok",
    "Plan",
    "Projection",
    "QuestionSpec",
    "Read",
    "RecordReader",
    "Reducer",
    "RelationKind",
    "RelationRequest",
    "ResumePolicy",
    "RetryPolicy",
    "ReturnedText",
    "RuleClassificationModel",
    "RuleExtractionModel",
    "RuleGenerationModel",
    "SandboxBackendSpec",
    "SandboxNeeds",
    "SandboxPolicy",
    "SaveRequest",
    "SpanDraft",
    "SpanRef",
    "StopPolicyOptIn",
    "SummaryPolicy",
    "SystemOfRecord",
    "SystemOfRecordSpec",
    "SystemReceipt",
    "Task",
    "TaskView",
    "TodoReminder",
    "ToolBody",
    "ToolCall",
    "ToolContext",
    "ToolDecl",
    "ToolOutcome",
    "ToolSpec",
    "ToolUse",
    "TreeManifest",
    "Unknown",
    "Usage",
    "VersionedSourceSpec",
    "artifact_keys",
    "decimal_string",
    "distribution_ppm",
    "from_field",
    "maybe_unknown",
    "part_version",
    "ppm",
    "source_keys",
    "where_part",
]
