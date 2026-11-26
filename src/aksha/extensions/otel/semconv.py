"""The one module that spells OpenTelemetry names (ADR-P21 D3, D4, D9).

Pinned to the OpenTelemetry GenAI semantic conventions v0.1.0, with the general keys of
semantic conventions v1.45.0 (``error.type``). Every span-name template, operation value
and attribute key the trace uses is a constant here, and :data:`EVENT_MAP` maps every
core event type to the span, span event or attribute it becomes. A convention rename
touches this file only; old runs re-export because the trace is rebuilt from events.

``aksha.*`` names cover what the conventions lack. The tenant never enters an ID or an
attribute; ``aksha.*`` attributes carry IDs, rule names and numbers, never document text.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

SEMCONV = "otel-genai/0.1.0"
"""Every exported resource carries ``aksha.semconv`` with this value (D3)."""

INSTRUMENTATION_SCOPE = "aksha.extensions.otel"

# --- operation values and span names (D2) --------------------------------------------------

OP_WORKFLOW = "invoke_workflow"
OP_AGENT = "invoke_agent"
OP_CHAT = "chat"
OP_TOOL = "execute_tool"
OP_EXTRACT = "extract"
OP_CLASSIFY = "classify"

RUN_SPAN = "invoke_workflow {agent}"
TASK_SPAN = "invoke_agent {agent}"
TURN_SPAN = "aksha.turn {instance}/t{turn:02d}"
MODEL_SPAN = "{operation} {model}"
TOOL_SPAN = "execute_tool {tool}"
STOP_SPAN = "aksha.stop_policy.evaluate {scope}"
INTERRUPT_SPAN = "aksha.interrupt {kind}"

MODEL_OPERATIONS: Mapping[str, str] = {
    "generation": OP_CHAT,
    "extraction": OP_EXTRACT,
    "classification": OP_CLASSIFY,
}
"""The operation of a model call by model class; adapted fast-class calls stay ``chat``."""

# --- attribute keys (D3) -------------------------------------------------------------------

GEN_AI_OPERATION = "gen_ai.operation.name"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_CONVERSATION = "gen_ai.conversation.id"
GEN_AI_PROVIDER = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GEN_AI_TOOL_RESULT = "gen_ai.tool.call.result"
GEN_AI_COMPACTED = "gen_ai.conversation.compacted"
ERROR_TYPE = "error.type"

AKSHA_SEMCONV = "aksha.semconv"
AKSHA_AGENT_VERSION = "aksha.agent.version"
AKSHA_RUN = "aksha.run.id"
AKSHA_SEGMENT = "aksha.segment"
AKSHA_SEGMENT_OUTCOME = "aksha.segment.outcome"
AKSHA_RUN_OUTCOME = "aksha.run.outcome"
AKSHA_TASK_ID = "aksha.task.id"
AKSHA_TURN = "aksha.turn.index"
AKSHA_STEP = "aksha.step.id"
AKSHA_STEP_OUTCOME = "aksha.step.outcome"
AKSHA_MODEL_CLASS = "aksha.model.class"
AKSHA_MODEL_ALIAS = "aksha.model.alias"
AKSHA_MODEL_NATIVE = "aksha.model.native"
AKSHA_STOP_VERDICT = "aksha.stop.verdict"
AKSHA_STOP_CAUSE = "aksha.stop.cause"
AKSHA_STOP_REASON = "aksha.stop.reason"
AKSHA_INTERRUPT_ID = "aksha.interrupt.id"
AKSHA_INTERRUPT_KIND = "aksha.interrupt.kind"
AKSHA_INTERRUPT_STATE = "aksha.interrupt.state"
AKSHA_APPROVAL_SIGNATURE = "aksha.approval.signature"
AKSHA_POLICY_RULE = "aksha.policy.rule"
AKSHA_ARTIFACT_REF = "aksha.artifact.ref"
AKSHA_RETRY_COUNT = "aksha.retry.count"
AKSHA_HEADLESS = "aksha.headless"
AKSHA_EVENT_TYPE = "aksha.event.type"
AKSHA_ATTEMPT_N = "aksha.attempt.n"
AKSHA_LINK = "aksha.link"
SERVICE_NAME = "service.name"
SERVICE_INSTANCE = "service.instance.id"

PROVIDER_FAKE = "aksha.fake"
"""``gen_ai.provider.name`` of an in-process fake or scripted model (no usage recorded)."""

REFUSED_ERROR = "aksha.refused"
"""``error.type`` of a tool call the policy gate refused."""

# --- span events ---------------------------------------------------------------------------

EV_ATTEMPT = "aksha.attempt"
EV_STOP_REQUESTED = "aksha.stop_requested"
EV_ARTIFACT_SAVED = "aksha.artifact.saved"
EV_INTERRUPT_RESOLVED = "aksha.interrupt.resolved"
EV_UNMAPPED = "aksha.unmapped {type}"
"""An event type with no row in :data:`EVENT_MAP`; a test fails on it (D9)."""

LINK_RESUMED_FROM = "resumed_from"

# --- content capture (D5) ------------------------------------------------------------------

CAPTURE_ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT"
CAPTURE_VALUES = ("NO_CONTENT", "SPAN_ONLY", "EVENT_ONLY", "SPAN_AND_EVENT")
CAPTURE_DEFAULT = "NO_CONTENT"


def capture_mode(value: str | None) -> str:
    """The capture mode of the environment value: case-insensitive, unknown is NO_CONTENT."""
    upper = (value or "").strip().upper()
    return upper if upper in CAPTURE_VALUES else CAPTURE_DEFAULT


# --- the mapping table (D9) ----------------------------------------------------------------

Role = Literal["span", "span_event", "attribute"]

EVENT_MAP: Mapping[str, tuple[Role, str]] = {
    # The span tree.
    "run.started": ("span", RUN_SPAN),
    "run.ended": ("span", RUN_SPAN),
    "task.entered": ("span", TASK_SPAN),
    "step.started": ("span", "model or tool span, and its turn"),
    "step.completed": ("span", "model or tool span, and its turn"),
    "step.failed": ("span", "model or tool span, and its turn"),
    "stop.decided": ("span", STOP_SPAN),
    "interrupt.raised": ("span", INTERRUPT_SPAN),
    "interrupt.resolved": ("span", INTERRUPT_SPAN),
    "interrupt.voided": ("span", INTERRUPT_SPAN),
    # Attributes of a span in the tree.
    "approval.requested": ("attribute", AKSHA_APPROVAL_SIGNATURE),
    "context.compacted": ("attribute", GEN_AI_COMPACTED),
    # Facts with no duration: span events on the innermost span that holds them.
    "step.dispatched": ("span_event", EV_ATTEMPT),
    "attempt.discarded": ("span_event", "aksha.attempt.discarded"),
    "artifact.saved": ("span_event", EV_ARTIFACT_SAVED),
    "read.recorded": ("span_event", "aksha.read.recorded"),
    "relation.saved": ("span_event", "aksha.relation.saved"),
    "excerpts.minted": ("span_event", "aksha.excerpts.minted"),
    "excerpts.shown": ("span_event", "aksha.excerpts.shown"),
    "citation.bound": ("span_event", "aksha.citation.bound"),
    "recovery.completed": ("span_event", "aksha.boot"),
    "outbox.enqueued": ("span_event", "aksha.effect.enqueued"),
    "outbox.claimed": ("span_event", "aksha.effect.claimed"),
    "outbox.failed": ("span_event", "aksha.effect.failed"),
    "outbox.completed": ("span_event", "aksha.effect.completed"),
    "outbox.abandoned": ("span_event", "aksha.effect.abandoned"),
    "command.accepted": ("span_event", "aksha.command"),
    "command.refused": ("span_event", "aksha.command.refused"),
    "budget.reached": ("span_event", "aksha.budget.reached"),
    "budget.granted": ("span_event", "aksha.budget.granted"),
    "run.finalizing": ("span_event", "aksha.run.finalizing"),
    "run.stop_requested": ("span_event", EV_STOP_REQUESTED),
    "source.changed": ("span_event", "aksha.source.changed"),
    "query.reexecuted": ("span_event", "aksha.query.reexecuted"),
    "approval.revoked": ("span_event", "aksha.approval.revoked"),
    "approval.granted": ("span_event", "aksha.approval.granted"),
    "approval.rejected": ("span_event", "aksha.approval.rejected"),
    "approval.voided": ("span_event", "aksha.approval.voided"),
    "review.accepted": ("span_event", "aksha.review.accepted"),
    "review.rejected": ("span_event", "aksha.review.rejected"),
    "task.withdrawn": ("span_event", "aksha.task.withdrawn"),
    "completion_check.evaluated": ("span_event", "aksha.completion_check"),
    "completion_check.reset": ("span_event", "aksha.completion_check.reset"),
    "keys.listed": ("span_event", "aksha.keys.listed"),
    "user.message": ("span_event", "aksha.user.message"),
    "route.decided": ("span_event", "aksha.route.decided"),
    "timer.scheduled": ("span_event", "aksha.timer.scheduled"),
    "timer.fired": ("span_event", "aksha.timer.fired"),
    "timer.cancelled": ("span_event", "aksha.timer.cancelled"),
    "source.expired": ("span_event", "aksha.source.expired"),
}
"""Every core event type and what it becomes in the trace (D9)."""
