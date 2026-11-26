"""The event type catalogue: ``EventTypeSpec``, ``Catalogue`` and the core event types.

Each event type has a JSON Schema, one writer, the transaction boundaries it may appear
in and an optional dedup-key template (ADR-P01 D4, D5). Payloads are validated here on
append, and the 64 KiB payload cap (D7) is checked here.

How the catalogue grows:

1. A core primitive adds its event types to ``CORE_EVENT_TYPES`` when it is built, in a
   section headed by its ADR, using the D8 names (``stop.decided``, ``interrupt.raised``,
   ``outbox.enqueued``, ``task.entered``). P01 itself declares only the four types the
   event store and recovery need; P02 declares the five step facts, which the policy
   gate's records (P11) also use.
2. An extension declares namespaced types (``memory.recalled``) in
   ``Extension.event_types``; the agent loader registers them through
   :meth:`Catalogue.register_extension`. The name carries the extension's namespace, the
   extension is the writer, it may not use a core namespace or a core dedup prefix, and no
   core reducer reads its types.
"""

from __future__ import annotations

import copy
import string
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from aksha.runtime.canonical import NotCanonical, canonical_bytes, canonical_json, nul_path
from aksha.runtime.errors import AkshaError
from aksha.runtime.events.boundaries import boundary
from aksha.runtime.events.errors import (
    BoundaryNotAllowed,
    CatalogueError,
    DedupKeyError,
    PayloadInvalid,
    PayloadTooLarge,
    UnknownEventType,
)
from aksha.runtime.events.stubs import Path, stub_paths, under

_INPUT_ID = {"type": "string", "pattern": "^IN-[0-9]{4,}$"}
"""An input ID (ADR-P27), used by ``input.*`` and ``stop.decided{inputs}``."""

PAYLOAD_CAP_BYTES = 64 * 1024
"""The largest payload, in bytes of canonical JSON (ADR-P01 D7; a placeholder to measure)."""


@dataclass(frozen=True)
class EventTypeSpec:
    """One event type: its payload schema, its one writer and where it may be appended.

    ``dedup_key`` is a ``str.format`` template over the envelope (``run``, ``owner``,
    ``session``, ``instance``, ``step``) and the payload's top-level fields, starting with
    a literal prefix that ends in ``:``, for example ``"stop:{run}/w{walk:03d}"``.
    """

    name: str
    schema: Mapping[str, Any]
    writer: str
    boundaries: frozenset[str]
    dedup_key: str | None = None
    schema_version: int = 1

    @property
    def namespace(self) -> str:
        """The part of the name before the first dot: ``stop`` in ``stop.decided``."""
        return self.name.split(".", 1)[0]

    @property
    def dedup_prefix(self) -> str | None:
        """The template's literal prefix, ``stop:`` in ``stop:{run}/w{walk:03d}``."""
        return None if self.dedup_key is None else self.dedup_key.split("{", 1)[0]


# --- P01: the event store and recovery -------------------------------------------------

RUN_STARTED = EventTypeSpec(
    name="run.started",
    schema={
        "type": "object",
        "properties": {
            "agent": {"type": "string", "minLength": 1},
            "cause": {"enum": ["request", "rebuild", "handoff", "timer"]},
            # ADR-P25 D2: a reminder's run names its timer and carries the request it states.
            "timer": {"type": "string", "minLength": 1},
            "request": {"type": "string", "minLength": 1},
            # ADR-P20 D7: the agent definition the run started under.
            "definition_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
            "extension_versions": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {"version": {"type": "string"}, "hash": {"type": "string"}},
                    "required": ["version", "hash"],
                },
            },
            "api_version": {"type": "string", "pattern": "^[0-9]+[.][0-9]+$"},
            "runtime_commit": {"type": "string"},
            "dirty": {"type": "boolean"},
            # ADR-P11 D12 (decision 26): a headless run never asks; every deny still holds.
            "headless": {"type": "boolean"},
            # ADR-P05 S1, D8: a rebuild run names the runs whose invalidated work it
            # regenerates, and its key (one rebuild run per agent and source version).
            "for_runs": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "rebuild_key": {"type": "string", "pattern": "^rebuild:"},
            # Decision 36: whether this run's re-entries may resume their session.
            "reentry": {"enum": ["fresh", "resume"]},
            # ADR-P07 D2, D9: the run mode and each alias's binding (alias, class, adapter
            # class, native), with its hash; never a configuration value or a key.
            "run_mode": {"enum": ["fake", "scripted", "replay", "live"]},
            "models": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string", "minLength": 1},
                        "model_class": {"enum": ["extraction", "classification", "generation"]},
                        "adapter": {"type": "string", "minLength": 1},
                        "native": {"type": "boolean"},
                    },
                    "required": ["alias", "model_class", "adapter", "native"],
                    "additionalProperties": False,
                },
            },
            "model_binding": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
            # ADR-P12 decision 9: the backend chosen at start, every probe, the run
            # ceiling's hash, whether the network is on and any granted image keys.
            "sandbox": {
                "type": "object",
                "properties": {
                    "backend": {"type": "string", "minLength": 1},
                    "strength": {"enum": ["none", "os-jail", "container", "vm"]},
                    "available": {"type": "boolean"},
                    "version": {"type": "string"},
                    "declared": {"type": "boolean"},
                    "probes": {"type": "array", "items": {"type": "object"}},
                    "ceiling_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                    "network": {"enum": ["on", "off"]},
                    "approved_images": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["probes", "network"],
            },
            # ADR-P14 D2: the run's budget; a dimension left out is not capped.
            "budget": {
                "type": "object",
                "properties": {
                    d: {"type": "integer", "minimum": 0}
                    for d in ("model_turns", "tool_calls", "active_ms", "tokens", "cost_micro_usd")
                },
                "additionalProperties": False,
            },
        },
        "required": ["agent", "cause"],
    },
    writer="session",
    # U4: the answer to a route question opens its run (ADR-P23 D4); U1 also holds a
    # reminder's run (ADR-P25 D2), opened by the timer that fired.
    boundaries=frozenset({"U1", "U4", "U9"}),
    dedup_key="rstart:{run}",
)
_UNKNOWN_ENTRY: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "minLength": 1},
        "reason": {"enum": ["unverified", "not_checked"]},
        "blocker": {"type": "string", "minLength": 1},
    },
    "required": ["key", "reason"],
    "additionalProperties": False,
}
RUN_ENDED = EventTypeSpec(
    name="run.ended",
    schema={
        "type": "object",
        "properties": {
            # ADR-P18 D2: ``stopped`` is a person's stop, never ``partial``. ADR-P24 D6:
            # ``partial`` ends with explicit unknowns, ``blocked`` with an untolerated block.
            "outcome": {"enum": ["complete", "partial", "blocked", "failed", "stopped"]},
            "reason": {"type": "string"},
            # ADR-P16 D3: the blocked instances a run ends with, for readers.
            "blockers": {"type": "array", "items": {"type": "string"}},
            # ADR-P24 D6: every explicit unknown, its reason and its blocker.
            "unknowns": {"type": "array", "items": _UNKNOWN_ENTRY},
        },
        "required": ["outcome", "reason"],
    },
    writer="stop_policy",
    boundaries=frozenset({"U3"}),
    dedup_key="rend:{run}",
)
_HASH = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}

# ADR-P04: one lineage input, as ``aksha.runtime.artifacts.types.InputRef.as_json`` writes it.
INPUT_REF_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"enum": ["record", "query", "statement", "artifact", "blob", "time"]},
        "id": {"type": "string", "minLength": 1},
        "version": {"type": "string", "minLength": 1},
        "path": {"type": "string"},
        "entry_hash": _HASH,
    },
    "required": ["kind", "id", "version"],
    "additionalProperties": False,
}

ARTIFACT_SAVED = EventTypeSpec(
    name="artifact.saved",
    schema={
        "type": "object",
        "properties": {
            "ref": {"type": "string", "minLength": 1},
            "kind": {"type": "string"},
            "key": {"type": ["string", "null"]},
            "version": {"type": "integer", "minimum": 1},
            "content_hash": _HASH,
            # ADR-P04 D5, D6: written by the artifact store's save path on every save.
            "canonical_hash": _HASH,
            "reads": {"type": "array", "items": INPUT_REF_SCHEMA},
            "reads_hash": _HASH,
            "primary_inputs": {"type": "array", "items": INPUT_REF_SCHEMA},
            "writer": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "minLength": 1},
                    "task_path": {"type": "string", "minLength": 1},
                    "key": {"type": ["string", "null"]},
                },
                "required": ["agent", "task_path", "key"],
                "additionalProperties": False,
            },
            "origin": {"enum": ["task", "derived", "choice", "copy_choice"]},
        },
        "required": ["ref", "kind", "key", "version", "content_hash"],
    },
    writer="artifacts",
    boundaries=frozenset({"U2", "U10"}),
    dedup_key="save:{ref}",
)
# --- P04 lineage inputs and typed relations (``artifacts/lineage.py``, ``relations.py``) --

READ_RECORDED = EventTypeSpec(
    name="read.recorded",
    schema={
        "type": "object",
        "properties": {"input": INPUT_REF_SCHEMA, "via": {"enum": ["context", "tool"]}},
        "required": ["input", "via"],
        "additionalProperties": False,
    },
    writer="artifacts",
    boundaries=frozenset({"U2"}),
)
_EXCERPT = {
    "type": "object",
    "properties": {
        "source": INPUT_REF_SCHEMA,
        "quote": {"type": "string", "minLength": 1},
        "pointer": {"type": "string"},
        # ADR-P13: the span a record's quote is verified against.
        "span": {"type": "string", "pattern": "^EX-[0-9]{4,}$"},
    },
    "required": ["source", "quote", "pointer"],
    "additionalProperties": False,
}
RELATION_SAVED = EventTypeSpec(
    name="relation.saved",
    schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "minLength": 1},
            "label": {"type": ["string", "null"]},
            "src": INPUT_REF_SCHEMA,
            "dst": INPUT_REF_SCHEMA,
            "evidence": {"type": "array", "items": _EXCERPT},
            "carrier": {"type": "string", "minLength": 1},
            "n": {"type": "integer", "minimum": 0},
        },
        "required": ["kind", "label", "src", "dst", "evidence", "carrier", "n"],
        "additionalProperties": False,
        # ADR-P09 D11: ``supersedes`` is lineage the runtime records, with no evidence; every
        # declared relation still cites at least one excerpt.
        "if": {"properties": {"kind": {"const": "supersedes"}}},
        "then": {"properties": {"evidence": {"maxItems": 0}}},
        "else": {"properties": {"evidence": {"minItems": 1}}},
    },
    writer="artifacts",
    boundaries=frozenset({"U2", "U10"}),
    dedup_key="rel:{carrier}#{n}",
)
# --- P13 grounding (``grounding/spans.py``, ``grounding/facts.py``) -----------------------

_SPAN = {
    "type": "object",
    "properties": {
        "span_id": {"type": "string", "pattern": "^EX-[0-9]{4,}$"},
        "source": {"type": "string", "minLength": 1},
        "record_id": {"type": "string", "minLength": 1},
        "version": {"type": "string", "minLength": 1},
        "start": {"type": "integer", "minimum": 0},
        "end": {"type": "integer", "minimum": 1},
        "text": {"type": "string", "minLength": 1},
        "tool": {"type": "string", "minLength": 1},
        "supersedes": {"type": "string", "pattern": "^EX-[0-9]{4,}$"},
    },
    "required": ["span_id", "source", "record_id", "version", "start", "end", "text", "tool"],
    "additionalProperties": False,
}
_SPAN_IDS = {"type": "array", "items": {"type": "string", "pattern": "^EX-[0-9]{4,}$"}}
EXCERPTS_MINTED = EventTypeSpec(
    name="excerpts.minted",
    schema={
        "type": "object",
        "properties": {
            "tool": {"type": "string", "minLength": 1},
            # D8: each span carries its full text, so an old quote can be re-verified.
            "spans": {"type": "array", "items": _SPAN},
            # S1: the spans whose text was inside the view shown to the envelope's instance.
            "shown": _SPAN_IDS,
        },
        "required": ["tool", "spans", "shown"],
        "additionalProperties": False,
    },
    writer="grounding",
    boundaries=frozenset({"U2", "U4"}),
)
EXCERPTS_SHOWN = EventTypeSpec(
    name="excerpts.shown",
    schema={
        "type": "object",
        "properties": {"spans": {**_SPAN_IDS, "minItems": 1}, "via": {"enum": ["context"]}},
        "required": ["spans", "via"],
        "additionalProperties": False,
    },
    writer="grounding",
    boundaries=frozenset({"U2"}),
)
CITATION_BOUND = EventTypeSpec(
    name="citation.bound",
    schema={
        "type": "object",
        "properties": {
            "carrier": {"type": "string", "minLength": 1},
            "n": {"type": "integer", "minimum": 0},
            "pointer": {"type": "string"},
            "form": {"enum": ["span", "statement", "artifact"]},
            "target": {"type": "string", "minLength": 1},
            "quote": {"type": "string", "minLength": 1},
            "occurrence": {"type": "integer", "minimum": 0},
            "path": {"type": "string"},
        },
        "required": ["carrier", "n", "pointer", "form", "target"],
        "additionalProperties": False,
    },
    writer="artifacts",
    boundaries=frozenset({"U2", "U10"}),
    dedup_key="cite:{carrier}#{n}",
)
RECOVERY_COMPLETED = EventTypeSpec(
    name="recovery.completed",
    schema={
        "type": "object",
        "properties": {"last_seq_before_boot": {"type": "integer", "minimum": 0}},
        "required": ["last_seq_before_boot"],
    },
    writer="execution",
    boundaries=frozenset({"U7"}),
)

# --- P08 summary compaction (``context/summary.py`` builds it) -------------------------
# D7: one recorded generation step replaces an instance's older turns with a summary. The
# summary rides on this event, never on an artifact, so it can never be cited as evidence;
# it commits with the summary step's ``step.completed`` (U2), so a resume replays it.

CONTEXT_COMPACTED = EventTypeSpec(
    name="context.compacted",
    schema={
        "type": "object",
        "properties": {
            "through_turn": {"type": "integer", "minimum": 1},
            "summary": {"type": "string", "minLength": 1},
            "summary_hash": _HASH,
            "policy_version": {"type": "integer", "minimum": 1},
        },
        "required": ["through_turn", "summary", "summary_hash", "policy_version"],
        "additionalProperties": False,
    },
    writer="context",
    boundaries=frozenset({"U2"}),
    dedup_key="compact:{step}",
)

# --- P02 step facts: the durable step executor (``execution/facts.py`` builds them) ------
# P11 D9: an allow is the ``step.started`` that follows; a refusal is one
# ``step.completed{outcome: refused, code, layer}``. The executor adds the step's name,
# semantics and recorded value, the dispatch marker of an at-most-once step, and the two
# attempt facts. ``duration_ms`` is read on the injected clock (a timed-out attempt records
# its timeout), never the real monotonic one, so two fake runs write the same bytes.

_REFUSED = {
    "if": {"properties": {"outcome": {"const": "refused"}}, "required": ["outcome"]},
    "then": {
        "properties": {
            "code": {"type": "string", "minLength": 1},
            "layer": {"type": "integer", "minimum": 0, "maximum": 7},
            "message": {"type": "string"},
            "counted": {"type": "boolean"},
            "detail": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["tool", "code", "layer", "message", "counted"],
    },
}

STEP_OUTCOMES = ("ok", "error", "refused", "interrupted", "skipped", "cancelled", "effects_unknown")
"""How a step ends; the last three are a person's stop (ADR-P18 D5, D6)."""
STEP_OUTCOMES_WITH_CODE = ("error", "interrupted", "skipped", "cancelled", "effects_unknown")
"""The step outcomes that must name a ``code``."""

STEP_STARTED = EventTypeSpec(
    name="step.started",
    schema={
        "type": "object",
        "properties": {
            "kind": {"enum": ["tool", "model", "check"]},
            "name": {"type": "string", "minLength": 1},
            "tool": {"type": "string", "minLength": 1},
            "args_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
            "semantics": {"enum": ["at_least_once", "reconcile", "at_most_once"]},
            "on_unknown": {"enum": ["tell_model", "interrupt"]},
            "position": {"type": "integer", "minimum": 0},
            "route": {"enum": ["dispatch", "outbox", "approval", "model", "reserved"]},
            "counted": {"type": "boolean"},
            # ADR-P03 D1: the idempotency key of the entry an effectful call enqueued.
            "outbox_key": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
            # ADR-P07: what answered a model step, and the cascade candidate it replaces.
            "model_class": {"enum": ["extraction", "classification", "generation"]},
            "alias": {"type": "string", "minLength": 1},
            "adapter": {"type": "string", "minLength": 1},
            "native": {"type": "boolean"},
            "replaces": {"type": "string", "minLength": 1},
            # ADR-P08 D3, S1: what the context of a generation step held. ``context_chars``
            # is its measured size; ``rendered`` each artifact shown and how; ``masked`` the
            # tool steps whose results it hid, so a later turn keeps them hidden (monotone).
            "context_chars": {"type": "integer", "minimum": 0},
            "rendered": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string", "minLength": 1},
                        "mode": {"enum": ["full", "summary", "ref_only"]},
                        "chars": {"type": "integer", "minimum": 0},
                        "spans": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["ref", "mode", "chars", "spans"],
                    "additionalProperties": False,
                },
            },
            "masked": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "summary_through": {"type": "integer", "minimum": 1},
        },
        "required": ["kind", "counted"],
    },
    writer="execution",
    boundaries=frozenset({"U2a"}),
    dedup_key="sstart:{step}",
)
STEP_COMPLETED = EventTypeSpec(
    name="step.completed",
    schema={
        "type": "object",
        "properties": {
            # ADR-P18 D5, D6: ``skipped`` is a call a stop kept from starting; ``cancelled``
            # and ``effects_unknown`` are a call the kill boundary ended.
            "outcome": {"enum": list(STEP_OUTCOMES)},
            "kind": {"enum": ["tool", "model", "check"]},
            "tool": {"type": "string", "minLength": 1},
            "name": {"type": "string", "minLength": 1},
            "code": {"type": "string", "minLength": 1},
            "value": {"type": "object"},
            "attempts": {"type": "integer", "minimum": 0},
            "on_unknown": {"enum": ["tell_model", "interrupt"]},
            "reconciled": {"type": "boolean"},
            "duration_ms": {"type": "integer", "minimum": 0},
            # ADR-P18 D6: whether the unit settled on request or was terminated.
            "kill": {"enum": ["settled", "killed"]},
            # ADR-P14 D9: what a model step reported spending.
            "usage": {
                "type": "object",
                "properties": {
                    "input_tokens": {"type": "integer", "minimum": 0},
                    "output_tokens": {"type": "integer", "minimum": 0},
                    "cost_micro_usd": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
        },
        "required": ["outcome"],
        "allOf": [
            _REFUSED,
            {
                "if": {"properties": {"outcome": {"enum": list(STEP_OUTCOMES_WITH_CODE)}}},
                "then": {"required": ["code"]},
            },
        ],
    },
    writer="execution",
    boundaries=frozenset({"U2"}),
    dedup_key="sdone:{step}",
)
STEP_DISPATCHED = EventTypeSpec(
    name="step.dispatched",
    schema={
        "type": "object",
        "properties": {"attempt": {"type": "integer", "minimum": 1}},
        "required": ["attempt"],
        "additionalProperties": False,
    },
    writer="execution",
    boundaries=frozenset({"U2b"}),
    dedup_key="sdisp:{step}",
)
STEP_FAILED = EventTypeSpec(
    name="step.failed",
    schema={
        "type": "object",
        "properties": {
            "attempt": {"type": "integer", "minimum": 1},
            "code": {"type": "string", "minLength": 1},
            "retried": {"type": "boolean"},
            "backoff_ms": {"type": "integer", "minimum": 0},
            "duration_ms": {"type": "integer", "minimum": 0},
        },
        "required": ["attempt", "code", "retried", "backoff_ms"],
        "additionalProperties": False,
    },
    writer="execution",
    boundaries=frozenset({"U2c"}),
    dedup_key="sfail:{step}/a{attempt}",
)
ATTEMPT_DISCARDED = EventTypeSpec(
    name="attempt.discarded",
    schema={
        "type": "object",
        "properties": {
            "attempt": {"type": "integer", "minimum": 1},
            "result_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        },
        "required": ["attempt", "result_hash"],
        "additionalProperties": False,
    },
    writer="execution",
    boundaries=frozenset({"U2c"}),
    dedup_key="sdisc:{step}/a{attempt}",
)

# --- P14 budgets and P16 stop policy: the stop decision record --------------------------
# ``stop.decided`` is the one record of a stop-policy evaluation (ADR-P16 D3). The budget
# and limit reducers read ``verdict``, ``cause``, ``candidate``, ``candidate_instance``,
# ``turn`` and ``resumes_turn`` (``budgets/records.py``): a CONTINUE charges a turn, a
# ``cause: resume`` record is a resume record (recovery writes it, ADR-P02 D7), a
# ``candidate: finish`` CONTINUE of the finishing instance is a veto. ``pops`` (and the
# older single ``pop``) are the instances the evaluation popped DONE or FAIL, which the
# Task status projection reads (ADR-P15 D6). The rest is the trail ``inspect`` and replay
# read: the stack as derived, each policy's verdict in order, who decided, which limit
# replaced a CONTINUE and the counts behind it. ``budget.reached`` commits with the budget
# interrupt in the stop decision; ``budget.granted`` is a person's command.

_DIMENSION = {"enum": ["model_turns", "tool_calls", "active_ms", "tokens", "cost_micro_usd"]}
_VERDICT_KIND = {"enum": ["pass", "continue", "push", "hold", "done", "fail", "stop"]}
_POLICY_VERDICT = {
    "type": "array",
    "prefixItems": [{"type": "string", "minLength": 1}, _VERDICT_KIND],
    "minItems": 2,
    "maxItems": 2,
}

STOP_DECIDED = EventTypeSpec(
    name="stop.decided",
    schema={
        "type": "object",
        "properties": {
            "walk": {"type": "integer", "minimum": 0},
            "verdict": {"enum": ["continue", "hold", "stop"]},
            "cause": {"type": "string", "pattern": "^[a-z][a-z0-9_:]*$"},
            "candidate": {"type": "string", "minLength": 1},
            "candidate_instance": {"type": "string", "minLength": 1},
            "after_turn": {"type": "integer", "minimum": 0},
            "seq_read": {"type": "integer", "minimum": 0},
            "turn": {"type": "integer", "minimum": 1},
            "resumes_turn": {"type": "boolean"},
            "reason": {"type": "string"},
            "limit": {"type": "string"},
            "counts": {"type": "object", "additionalProperties": {"type": "integer"}},
            # ADR-P15 D6: the decision popped its instance DONE or FAIL (``blocked``).
            "pop": {"enum": ["done", "fail"]},
            "pops": {
                "type": "array",
                "items": {
                    "type": "array",
                    "prefixItems": [{"type": "string", "minLength": 1}, {"enum": ["done", "fail"]}],
                    "minItems": 2,
                    "maxItems": 2,
                },
            },
            "stack": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "trail": {"type": "array", "items": _POLICY_VERDICT},
            "decided_by": {"type": "string", "minLength": 1},
            "replaced": _POLICY_VERDICT,
            "outcome": {"enum": ["complete", "partial", "blocked", "failed", "stopped"]},
            "interrupt": {"type": "string", "minLength": 1},
            # ADR-P27 D4: the person's pending inputs a CONTINUE turned a finish into.
            "inputs": {"type": "array", "items": _INPUT_ID, "minItems": 1},
        },
        "required": ["walk", "verdict"],
        "additionalProperties": False,
    },
    writer="stop_policy",
    boundaries=frozenset({"U3"}),
    dedup_key="stop:{run}/w{walk:03d}",
)
BUDGET_REACHED = EventTypeSpec(
    name="budget.reached",
    schema={
        "type": "object",
        "properties": {
            "interrupt_id": {"type": "string", "minLength": 1},
            "limit": _DIMENSION,
            "used": {"type": "integer", "minimum": 0},
            "allowed": {"type": "integer", "minimum": 0},
            "partial": {"type": "boolean"},
        },
        "required": ["interrupt_id", "limit", "used", "allowed", "partial"],
        "additionalProperties": False,
    },
    writer="stop_policy",
    boundaries=frozenset({"U3"}),
    dedup_key="breach:{run}/{interrupt_id}",
)
BUDGET_GRANTED = EventTypeSpec(
    name="budget.granted",
    schema={
        "type": "object",
        "properties": {
            "interrupt_id": {"type": "string", "minLength": 1},
            "by": {"type": "string", "minLength": 1},
            "extra": {
                "type": "object",
                "propertyNames": _DIMENSION,
                "additionalProperties": {"type": "integer", "minimum": 1},
                "minProperties": 1,
            },
        },
        "required": ["interrupt_id", "by", "extra"],
        "additionalProperties": False,
    },
    writer="session",
    boundaries=frozenset({"U4"}),
    dedup_key="bgrant:{run}/{interrupt_id}",
)
RUN_FINALIZING = EventTypeSpec(
    name="run.finalizing",
    schema={
        "type": "object",
        "properties": {
            # ADR-P24 D4: the budget interrupt "finalize" answered, and who answered it.
            "interrupt": {"type": "string", "minLength": 1},
            "by": {"type": "string", "minLength": 1},
            # D5: the reserve per capped dimension, which only finalizer Tasks spend.
            "reserve": {
                "type": "object",
                "propertyNames": _DIMENSION,
                "additionalProperties": {"type": "integer", "minimum": 0},
            },
            "finalizers": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "voided": {"type": "array", "items": {"type": "string", "minLength": 1}},
        },
        "required": ["interrupt", "by", "reserve", "finalizers", "voided"],
        "additionalProperties": False,
    },
    writer="session",
    boundaries=frozenset({"U4"}),
    dedup_key="rfinal:{run}",
)

# --- P03 the transactional outbox and command receipts (``outbox/facts.py``, ----------
# ``session/commands.py``). An outbox entry is ``outbox.enqueued``, committed in the
# transaction of the command that decided it. The worker's facts (claim, failed attempt,
# completion, abandonment) are each one U6 transaction, except that an effectful tool
# step's entry commits with its ``step.started`` (U2a) and ends with its ``step.completed``
# (U2), so the step's result and the entry's end commit together. ``done-outbox:`` is shared by the
# completion and the abandonment, so an entry ends once. ``cmd:`` is shared by
# ``command.accepted`` and ``command.refused``, so a command ID has one receipt.

_KEY = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
_SNAKE = {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"}
_COMMAND_BOUNDARIES = frozenset({"U1", "U4", "U5", "U8"})

OUTBOX_ENQUEUED = EventTypeSpec(
    name="outbox.enqueued",
    schema={
        "type": "object",
        "properties": {
            "key": _KEY,
            "tool": {"type": "string", "minLength": 1},
            "system": {"type": ["string", "null"]},
            "semantics": {"enum": ["at_least_once", "reconcile", "at_most_once"]},
            "on_unknown": {"enum": ["tell_model", "interrupt"]},
            "request": {"type": "object"},
            "request_hash": _KEY,
            "approval_id": {"type": "string", "minLength": 1},
            "command_id": {"type": "string", "minLength": 1},
            # ADR-P29 D1: the outside record the request revises, and its source versions.
            "record": {
                "type": "object",
                "properties": {"key": _KEY, "fields": {"type": "object"}},
                "required": ["key", "fields"],
                "additionalProperties": False,
            },
            "sources": {"type": "array", "items": {"type": "string", "minLength": 1}},
        },
        "dependentRequired": {"record": ["sources"], "sources": ["record"]},
        "required": [
            "key",
            "tool",
            "system",
            "semantics",
            "on_unknown",
            "request",
            "request_hash",
        ],
        "additionalProperties": False,
    },
    writer="outbox",
    boundaries=frozenset({"U2a", "U4", "U5"}),
    dedup_key="outbox:{key}",
)
OUTBOX_CLAIMED = EventTypeSpec(
    name="outbox.claimed",
    schema={
        "type": "object",
        "properties": {"key": _KEY, "attempt": {"type": "integer", "minimum": 1}},
        "required": ["key", "attempt"],
        "additionalProperties": False,
    },
    writer="outbox",
    boundaries=frozenset({"U6"}),
    dedup_key="oclaim:{key}/a{attempt}",
)
OUTBOX_FAILED = EventTypeSpec(
    name="outbox.failed",
    schema={
        "type": "object",
        "properties": {
            "key": _KEY,
            "attempt": {"type": "integer", "minimum": 1},
            "code": _SNAKE,
            "retried": {"type": "boolean"},
            "backoff_ms": {"type": "integer", "minimum": 0},
        },
        "required": ["key", "attempt", "code", "retried", "backoff_ms"],
        "additionalProperties": False,
    },
    writer="outbox",
    boundaries=frozenset({"U6"}),
    dedup_key="ofail:{key}/a{attempt}",
)
OUTBOX_COMPLETED = EventTypeSpec(
    name="outbox.completed",
    schema={
        "type": "object",
        "properties": {
            "key": _KEY,
            "ledger_id": {"type": "string", "minLength": 1},
            "receipt": {"type": "object"},
            "attempts": {"type": "integer", "minimum": 0},
            "reconciled": {"type": "boolean"},
        },
        "required": ["key", "ledger_id", "receipt", "attempts"],
        "additionalProperties": False,
    },
    writer="outbox",
    boundaries=frozenset({"U2", "U6"}),
    dedup_key="done-outbox:{key}",
)
OUTBOX_ABANDONED = EventTypeSpec(
    name="outbox.abandoned",
    schema={
        "type": "object",
        "properties": {
            "key": _KEY,
            "reason": _SNAKE,
            "attempts": {"type": "integer", "minimum": 0},
            "on_unknown": {"enum": ["tell_model", "interrupt"]},
        },
        "required": ["key", "reason", "attempts"],
        "additionalProperties": False,
    },
    writer="outbox",
    boundaries=frozenset({"U2", "U6"}),
    dedup_key="done-outbox:{key}",
)
COMMAND_ACCEPTED = EventTypeSpec(
    name="command.accepted",
    schema={
        "type": "object",
        "properties": {
            "command_id": {"type": "string", "minLength": 1},
            "name": _SNAKE,
            "args_hash": _KEY,
            "answers": {"type": "string", "minLength": 1},
        },
        "required": ["command_id", "name", "args_hash"],
        "additionalProperties": False,
    },
    writer="session",
    boundaries=_COMMAND_BOUNDARIES,
    dedup_key="cmd:{owner}/{command_id}",
)
COMMAND_REFUSED = EventTypeSpec(
    name="command.refused",
    schema={
        "type": "object",
        "properties": {
            "command_id": {"type": "string", "minLength": 1},
            "name": _SNAKE,
            "args_hash": _KEY,
            "answers": {"type": "string", "minLength": 1},
            "code": _SNAKE,
            "message": {"type": "string"},
        },
        "required": ["command_id", "name", "args_hash", "code"],
        "additionalProperties": False,
    },
    writer="session",
    boundaries=_COMMAND_BOUNDARIES,
    dedup_key="cmd:{owner}/{command_id}",
)

# --- P18 cancellation ------------------------------------------------------------------
# A person's stop is a fact the ``stop`` command commits with its receipt (U4); the stop
# ending voids the run's open interrupts in the stop decision (U3). ``interrupt.voided``
# is ADR-P17's event; cancellation is its first writer, and P17 widens its boundaries.

RUN_STOP_REQUESTED = EventTypeSpec(
    name="run.stop_requested",
    schema={
        "type": "object",
        "properties": {
            "scope": {"enum": ["run", "turn"]},
            "reason": {"type": "string", "minLength": 1},
            "by": {"type": "string", "minLength": 1},
            "command_id": {"type": "string", "minLength": 1},
        },
        "required": ["scope", "reason", "by", "command_id"],
        "additionalProperties": False,
    },
    writer="session",
    boundaries=frozenset({"U4"}),
    dedup_key="rstop:{run}/{command_id}",
)
INTERRUPT_VOIDED = EventTypeSpec(
    name="interrupt.voided",
    schema={
        "type": "object",
        "properties": {
            "interrupt": {"type": "string", "minLength": 1},
            "reason": _SNAKE,
        },
        "required": ["interrupt", "reason"],
        "additionalProperties": False,
    },
    writer="stop_policy",
    # U3: the stop ending; U2, U4, U8: invalidation voids a diff review whose revision went
    # stale in the transaction that confirmed it (ADR-P05 D9).
    boundaries=frozenset({"U2", "U3", "U4", "U8"}),
    dedup_key="ivoid:{run}/{interrupt}",
)

# --- P05 invalidation (``invalidation/``) ------------------------------------------------
# The stored facts of invalidation are the source change, the query re-executions, the
# saves and the revocations; every invalidation set is a projection over them (D3).
# ``interrupt.raised``, ``interrupt.resolved`` and ``approval.requested`` are ADR-P17's
# types; invalidation is their first core writer (a diff review and the fresh approval
# request an acceptance makes). P17 gave them their full schemas: an approval request
# carries its action body and signature, and ``route`` is the one session-scoped kind.

_REF_TEXT = {"type": "string", "minLength": 1}
_SIGNATURE = {"type": "string", "pattern": "^v[0-9]+:[0-9a-f]{64}$"}
_ACTION_BODY = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "minLength": 1},
        "owner": {"type": "string", "minLength": 1},
        "canonical_args": {"type": "string"},
        "targets": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2},
        },
        "idempotency_key": {"type": "string", "minLength": 1},
        "definition_hash": {"type": "string", "minLength": 1},
        "facts": {"type": "object", "additionalProperties": {"type": "string"}},
    },
    "required": [
        "tool",
        "owner",
        "canonical_args",
        "targets",
        "idempotency_key",
        "definition_hash",
        "facts",
    ],
    "additionalProperties": False,
}
_HUNK = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "old": {}, "new": {}},
    "required": ["path"],
    "additionalProperties": False,
}
_READ_STATUS = {
    "type": "object",
    "properties": {
        "input": INPUT_REF_SCHEMA,
        "state": {"enum": ["same", "equivalent", "moved", "unsettled", "gone"]},
        "now": {"type": ["string", "null"]},
    },
    "required": ["input", "state", "now"],
    "additionalProperties": False,
}

SOURCE_CHANGED = EventTypeSpec(
    name="source.changed",
    schema={
        "type": "object",
        "properties": {
            "source_version": {"type": "integer", "minimum": 1},
            "cause": {"enum": ["command", "recovery", "clock"]},
            "change_set": {"type": "string"},
            "sources": {"type": "object", "additionalProperties": {"type": "string"}},
            "records": {
                "type": "object",
                "additionalProperties": {"type": ["string", "null"]},
            },
        },
        "required": ["source_version", "cause", "sources", "records"],
        "additionalProperties": False,
    },
    writer="invalidation",
    boundaries=frozenset({"U8"}),
)
QUERY_REEXECUTED = EventTypeSpec(
    name="query.reexecuted",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "source_version": {"type": "integer", "minimum": 1},
            "previous": {"type": "string", "minLength": 1},
            "hash": {"type": "string", "minLength": 1},
            "changed": {"type": "boolean"},
        },
        "required": ["query", "source_version", "previous", "hash", "changed"],
        "additionalProperties": False,
    },
    writer="invalidation",
    boundaries=frozenset({"U8"}),
)
APPROVAL_REVOKED = EventTypeSpec(
    name="approval.revoked",
    schema={
        "type": "object",
        "properties": {
            "approval": {"type": "string", "minLength": 1},
            "target": _REF_TEXT,
            "moved": {"type": "array", "items": INPUT_REF_SCHEMA},
        },
        "required": ["approval", "target", "moved"],
        "additionalProperties": False,
    },
    writer="invalidation",
    boundaries=frozenset({"U2", "U4", "U8"}),
    dedup_key="revoke:{approval}",
)
INTERRUPT_RAISED = EventTypeSpec(
    name="interrupt.raised",
    schema={
        "type": "object",
        "properties": {
            "interrupt": {"type": "string", "minLength": 1},
            # ADR-P16 D3: ``budget`` is the budget interrupt a stop decision raises (U3).
            "kind": {"enum": ["input", "choice", "approval", "review", "budget", "route"]},
            # ADR-P17: what an interrupt shows. ``questions`` (S2, one answer each),
            # ``options`` (a choice; the agents a route lists), the agent's ``label``, and an
            # approval's ID, signature, tool, facts and targets (its body is on the request).
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "text": {"type": "string"},
                    },
                    "required": ["id", "text"],
                    "additionalProperties": False,
                },
            },
            "options": {"type": "array", "items": {"type": "string"}},
            "label": {"type": "string"},
            "approval": {"type": "string", "minLength": 1},
            "signature": {"type": "string", "pattern": "^v[0-9]+:[0-9a-f]{64}$"},
            "tool": {"type": "string", "minLength": 1},
            "facts": {"type": "object", "additionalProperties": {"type": "string"}},
            "targets": {"type": "array", "items": {"type": "string"}},
            # ADR-P05: a diff review's card, and the choice a rejected review raises (S4).
            "old": _REF_TEXT,
            "revision": _REF_TEXT,
            "card_hash": _HASH,
            "changed_reads": {"type": "array", "items": _READ_STATUS},
            "via": {"type": "array", "items": {"type": "string"}},
            "hunks": {"type": "array", "items": _HUNK},
            "writer": {"type": "object"},
            "reason": _SNAKE,
            # ADR-P23 D3: a route question's message, each listed agent's purpose line, and
            # the two or three highest-scoring agents (the rest follow in ``options``).
            "message": {"type": "string", "pattern": "^M-[0-9]{4,}$"},
            "purposes": {"type": "object", "additionalProperties": {"type": "string"}},
            "suggested": {"type": "array", "items": {"type": "string", "minLength": 1}},
        },
        "required": ["interrupt"],
        "allOf": [
            {
                "if": {"properties": {"kind": {"const": "review"}}, "required": ["kind"]},
                "then": {"required": ["old", "revision", "card_hash", "hunks"]},
            },
            {
                "if": {"properties": {"kind": {"const": "approval"}}, "required": ["kind"]},
                "then": {"required": ["approval", "signature"]},
            },
        ],
    },
    writer="interrupts",
    # U3: the budget interrupt, raised in the stop decision that a budget limit bit.
    boundaries=frozenset({"U2", "U3", "U4"}),
)
INTERRUPT_RESOLVED = EventTypeSpec(
    name="interrupt.resolved",
    schema={
        "type": "object",
        "properties": {
            "interrupt": {"type": "string", "minLength": 1},
            # ADR-P17 D4: who answered, the answer per question ID (S2), or the decision.
            "by": {"type": "string", "minLength": 1},
            "answers": {"type": "object", "additionalProperties": {"type": "string"}},
            # ADR-P24 D4: ``finalized`` answers a budget interrupt with ``finalize``.
            "decision": {"enum": ["granted", "rejected", "finalized"]},
        },
        "required": ["interrupt"],
    },
    writer="session",
    # No dedup key: a budget interrupt's ID is unique only within its run. The answer
    # command refuses a closed interrupt ``already_decided``, and every kind-specific
    # decision fact carries its own key (``decide:``, ``review:``, ``bgrant:``).
    boundaries=frozenset({"U4"}),
)
REVIEW_ACCEPTED = EventTypeSpec(
    name="review.accepted",
    schema={
        "type": "object",
        "properties": {
            "interrupt": {"type": "string", "minLength": 1},
            "old": _REF_TEXT,
            "revision": _REF_TEXT,
            "card_hash": _HASH,
        },
        "required": ["interrupt", "old", "revision", "card_hash"],
        "additionalProperties": False,
    },
    writer="session",
    boundaries=frozenset({"U4"}),
    dedup_key="review:{interrupt}",
)
REVIEW_REJECTED = EventTypeSpec(
    name="review.rejected",
    schema=REVIEW_ACCEPTED.schema,
    writer="session",
    boundaries=frozenset({"U4"}),
    dedup_key="review:{interrupt}",
)
APPROVAL_REQUESTED = EventTypeSpec(
    name="approval.requested",
    schema={
        "type": "object",
        "properties": {
            "approval": {"type": "string", "minLength": 1},
            "targets": {"type": "array", "items": _REF_TEXT},
            "replaces": {"type": "string", "minLength": 1},
            # ADR-P17 D5, D6: the generic action body and its approval signature.
            "body": _ACTION_BODY,
            "signature": _SIGNATURE,
        },
        "required": ["approval", "body", "signature"],
    },
    writer="interrupts",
    boundaries=frozenset({"U2", "U4"}),
)

# --- P17 interrupts and approvals (``interrupts/``) --------------------------------------
# A person's decision on an approval commits with its command (U4) under ``decide:``, so a
# second decision is ``already_decided``. ``approval.voided`` is the runtime's: an edit
# (U4) or a withdrawn key (U3, S3). ``approval.revoked`` is invalidation's (above).

APPROVAL_GRANTED = EventTypeSpec(
    name="approval.granted",
    schema={
        "type": "object",
        "properties": {
            "approval": {"type": "string", "minLength": 1},
            "signature": _SIGNATURE,
            "by": {"type": "string", "minLength": 1},
            "targets": {"type": "array", "items": _REF_TEXT},
        },
        "required": ["approval", "signature", "by", "targets"],
        "additionalProperties": False,
    },
    writer="session",
    boundaries=frozenset({"U4"}),
    dedup_key="decide:{approval}",
)
APPROVAL_REJECTED = EventTypeSpec(
    name="approval.rejected",
    schema={
        "type": "object",
        "properties": {
            "approval": {"type": "string", "minLength": 1},
            "signature": _SIGNATURE,
            "by": {"type": "string", "minLength": 1},
            "note": {"type": "string"},
        },
        "required": ["approval", "signature", "by"],
        "additionalProperties": False,
    },
    writer="session",
    boundaries=frozenset({"U4"}),
    dedup_key="decide:{approval}",
)
APPROVAL_VOIDED = EventTypeSpec(
    name="approval.voided",
    schema={
        "type": "object",
        "properties": {
            "approval": {"type": "string", "minLength": 1},
            # ADR-P25 D5: ``expired``, its approval_expiry timer fired.
            "reason": {"enum": ["replaced_by_edit", "key_withdrawn", "superseded", "expired"]},
        },
        "required": ["approval", "reason"],
        "additionalProperties": False,
    },
    writer="interrupts",
    boundaries=frozenset({"U3", "U4"}),
    dedup_key="avoid:{approval}",
)

# --- P15 Task model and scheduler (``tasks/``) ------------------------------------------
# Every fact here commits inside a stop decision (U3), so no Task fact has a second writer;
# a person's answer resets a blocked instance in its resolution (U4), and a source change
# may list a collection's keys again (U8). Status is a projection over them (D6).

_TASK_KEY: Mapping[str, Any] = {"type": ["string", "null"]}
_TASK_PATH: Mapping[str, Any] = {
    "type": "string",
    "pattern": "^[a-z][a-z0-9_]*(/[a-z][a-z0-9_]*)*$",
}
_COUNT: Mapping[str, Any] = {"type": "integer", "minimum": 0}
_FRESH_REASONS = frozenset(
    {
        "resume_disabled", "session_missing", "sessions_split", "definition_changed",
        "prior_failed", "prior_looping", "prior_overflow", "tainted_session",
        "premise_changed", "mostly_stale", "budget_exceeded",
    }
)  # fmt: skip
"""The re-entry fresh reasons (decision 36): ``context.reentry.FRESH_REASONS``, kept equal."""
_REENTRY: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {"enum": ["fresh", "resume"]},
        "reason": {"enum": sorted(_FRESH_REASONS)},
        "setting": {"enum": ["fresh", "resume"]},
        "from_turn": {"type": "integer", "minimum": 1},
        "as_of_seq": _COUNT,
        "session": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "run": {"type": "string", "minLength": 1},
                    "instance": {"type": "string", "minLength": 1},
                    "from_turn": {"type": "integer", "minimum": 1},
                    "through_turn": {"type": "integer", "minimum": 1},
                },
                "required": ["run", "instance", "from_turn", "through_turn"],
                "additionalProperties": False,
            },
        },
        "read_runs": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "new_reads": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "session_turns": _COUNT,
        "session_chars": _COUNT,
        "stale_chars": _COUNT,
    },
    "required": ["mode", "setting", "from_turn", "as_of_seq", "session", "read_runs"],
    "additionalProperties": False,
}
TASK_ENTERED = EventTypeSpec(
    name="task.entered",
    schema={
        "type": "object",
        "properties": {
            "cause": {"enum": ["scheduler", "rebuild"]},
            "task": _TASK_PATH,
            "key": _TASK_KEY,
            "writer": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "minLength": 1},
                    "task_path": _TASK_PATH,
                    "key": _TASK_KEY,
                },
                "required": ["agent", "task_path", "key"],
                "additionalProperties": False,
            },
            "key_source": {
                "type": "object",
                "properties": {
                    "origin": {"enum": ["source", "artifact"]},
                    "name": {"type": "string", "minLength": 1},
                    "path": {"type": "string"},
                },
                "required": ["origin", "name"],
                "additionalProperties": False,
            },
            # Decision 36: a re-entry's mode, why, and what its context reads.
            "reentry": _REENTRY,
        },
        "required": ["cause", "task", "key", "writer"],
        "additionalProperties": False,
    },
    writer="stop_policy",
    boundaries=frozenset({"U3"}),
)
TASK_WITHDRAWN = EventTypeSpec(
    name="task.withdrawn",
    schema={
        "type": "object",
        "properties": {"task": _TASK_PATH, "key": _TASK_KEY, "reason": {"const": "key_withdrawn"}},
        "required": ["task", "key", "reason"],
        "additionalProperties": False,
    },
    writer="stop_policy",
    boundaries=frozenset({"U3"}),
)
COMPLETION_CHECK_EVALUATED = EventTypeSpec(
    name="completion_check.evaluated",
    schema={
        "type": "object",
        "properties": {
            "task": _TASK_PATH,
            "key": _TASK_KEY,
            "on": {"enum": ["entry", "finish", "parent"]},
            "check": {"type": "string"},
            "ok": {"type": "boolean"},
            "message": {"type": "string"},
            "count": {"type": "integer", "minimum": 0},
            "keys": {"type": "array", "items": {"type": "string"}},
            "code": {
                "enum": [
                    "",
                    "check_failed",
                    "check_raised",
                    "children_pending",
                    "child_blocked",
                    "keys_pending",
                    "rebuild_pending",
                ]
            },
            "blockers": {"type": "array", "items": {"type": "string"}},
            "clears": {"enum": ["supervisor", "worker", "person"]},
        },
        "required": ["task", "key", "on", "ok", "message", "count", "keys", "code"],
        "additionalProperties": False,
    },
    writer="stop_policy",
    boundaries=frozenset({"U3"}),
)
COMPLETION_CHECK_RESET = EventTypeSpec(
    name="completion_check.reset",
    schema={
        "type": "object",
        "properties": {
            "task": _TASK_PATH,
            "key": _TASK_KEY,
            "cause": {"enum": ["answer", "rebuild"]},
        },
        "required": ["task", "key", "cause"],
        "additionalProperties": False,
    },
    writer="stop_policy",
    boundaries=frozenset({"U3", "U4"}),
)
KEYS_LISTED = EventTypeSpec(
    name="keys.listed",
    schema={
        "type": "object",
        "properties": {
            "collection": {"type": "string", "minLength": 1},
            "keys": {
                "type": "array",
                "items": {"type": "string", "pattern": "^[^/\\[\\]]+$"},
                "uniqueItems": True,
            },
        },
        "required": ["collection", "keys"],
        "additionalProperties": False,
    },
    writer="stop_policy",
    boundaries=frozenset({"U3", "U8"}),
)

# --- P23: the request router -----------------------------------------------------------
# A request reaches a session as ``user.message``; the router's decision of which agent
# handles it is ``route.decided``, written in the transaction that opens the run (D6).

USER_MESSAGE = EventTypeSpec(
    name="user.message",
    schema={
        "type": "object",
        "properties": {
            "message": {"type": "string", "pattern": "^M-[0-9]{4,}$"},
            "text": {"type": "string", "minLength": 1},
            # D5: the agent the person named; a message that names one skips the router.
            "agent": {"type": "string", "minLength": 1},
        },
        "required": ["message", "text"],
        "additionalProperties": False,
    },
    writer="session",
    boundaries=frozenset({"U1"}),
    dedup_key="msg:{message}",
)
ROUTE_DECIDED = EventTypeSpec(
    name="route.decided",
    schema={
        "type": "object",
        "properties": {
            "message": {"type": "string", "pattern": "^M-[0-9]{4,}$"},
            "agent": {"type": "string", "minLength": 1},
            "by": {"enum": ["classifier", "person"]},
            "run": {"type": "string", "pattern": "^R-[0-9]{4,}$"},
            # The classification step behind it; absent when a person named the agent.
            "step": {"type": "string", "minLength": 1},
            "probabilities_ppm": {
                "type": "object",
                "additionalProperties": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
            },
        },
        "required": ["message", "agent", "by", "run"],
        "additionalProperties": False,
    },
    writer="session",
    # U1: a named agent and a classifier's accepted answer; U4: a person's route answer.
    boundaries=frozenset({"U1", "U4"}),
    dedup_key="route:{message}",
)

# --- P25 durable timers and recorded time (``timers/``) ----------------------------------
# A timer is a fact (D1): ``timer.scheduled`` in the transaction that wants it, closed once
# by ``timer.fired`` or ``timer.cancelled`` (one dedup key for both). Each firing commits
# in its cause's boundary: an expiry with the approval it voids (U4), a recheck with the
# ``source.expired`` it writes (U8), a reminder with the run it opens (U1).

_TIMER_ID = {"type": "string", "pattern": "^[A-Za-z0-9][^\\s]*$"}
_TIMER_CAUSE = {"enum": ["approval_expiry", "reminder", "recheck"]}
_TIMER_SCOPE = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "kind": {"const": "instance"},
                "run": {"type": "string", "minLength": 1},
                "instance": {"type": "string", "minLength": 1},
            },
            "required": ["kind", "run", "instance"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"kind": {"const": "run"}, "run": {"type": "string", "minLength": 1}},
            "required": ["kind", "run"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "agent"},
                "agent": {"type": "string", "minLength": 1},
            },
            "required": ["kind", "agent"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "session"},
                "session": {"type": "string", "minLength": 1},
            },
            "required": ["kind", "session"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "approval"},
                "approval": {"type": "string", "minLength": 1},
            },
            "required": ["kind", "approval"],
            "additionalProperties": False,
        },
    ]
}

TIMER_SCHEDULED = EventTypeSpec(
    name="timer.scheduled",
    schema={
        "type": "object",
        "properties": {
            "timer_id": _TIMER_ID,
            "cause": _TIMER_CAUSE,
            "scope": _TIMER_SCOPE,
            # Exactly one: an ISO time on the injected clock, or (B3) milliseconds counted
            # from this fact's own recorded ``at``.
            "due_at": {"type": "string", "format": "date-time", "minLength": 20},
            "after_ms": {"type": "integer", "minimum": 0},
            # recheck (D3): the record read or the artifact version whose lifetime runs out.
            "target": INPUT_REF_SCHEMA,
            # reminder: the agent and the request its run states.
            "request": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "minLength": 1},
                    "text": {"type": "string", "minLength": 1},
                },
                "required": ["agent", "text"],
                "additionalProperties": False,
            },
        },
        "required": ["timer_id", "cause", "scope"],
        "oneOf": [{"required": ["due_at"]}, {"required": ["after_ms"]}],
        "additionalProperties": False,
    },
    writer="timers",
    # U2: a step that read, saved or asked for approval; U4: a fresh approval request;
    # U1 and U8: a reminder or a recheck scheduled with the facts that want it.
    boundaries=frozenset({"U1", "U2", "U4", "U8"}),
    dedup_key="timer:{timer_id}",
)
TIMER_FIRED = EventTypeSpec(
    name="timer.fired",
    schema={
        "type": "object",
        "properties": {
            "timer_id": _TIMER_ID,
            "cause": _TIMER_CAUSE,
            # reminder: the run it opened.
            "run": {"type": "string", "pattern": "^R-[0-9]{4,}$"},
        },
        "required": ["timer_id", "cause"],
        "additionalProperties": False,
    },
    writer="timers",
    boundaries=frozenset({"U1", "U4", "U8"}),
    dedup_key="tclose:{timer_id}",
)
TIMER_CANCELLED = EventTypeSpec(
    name="timer.cancelled",
    schema={
        "type": "object",
        "properties": {
            "timer_id": _TIMER_ID,
            # ``moot``: what it would act on is already settled (a decided approval).
            "reason": _SNAKE,
        },
        "required": ["timer_id", "reason"],
        "additionalProperties": False,
    },
    writer="timers",
    boundaries=frozenset({"U1", "U4", "U8"}),
    dedup_key="tclose:{timer_id}",
)
SOURCE_EXPIRED = EventTypeSpec(
    name="source.expired",
    schema={
        "type": "object",
        "properties": {
            "source_version": {"type": "integer", "minimum": 1},
            "cause": {"enum": ["command", "recovery", "clock"]},
            "records": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "artifacts": {"type": "array", "items": _REF_TEXT},
        },
        "required": ["source_version", "cause", "records", "artifacts"],
        "additionalProperties": False,
    },
    writer="invalidation",
    boundaries=frozenset({"U8"}),
)

# --- P27 steering and follow-up input (``inputs/``) -----------------------------------
# A person's message to an open run commits ``input.queued`` with its command receipt
# (U4). ``input.consumed`` commits with what rendered or applied it: a model step's
# result (U2), the stop decision whose finish it vetoed (U3) or the amendment's own
# transaction (U4). ``interrupt.amended`` records a corrected answer beside the statement
# span that supersedes the old one.

INPUT_QUEUED = EventTypeSpec(
    name="input.queued",
    schema={
        "type": "object",
        "properties": {
            "input_id": _INPUT_ID,
            "lane": {"enum": ["steer", "follow_up", "amend"]},
            "text": {"type": "string", "minLength": 1},
            "instance": {"type": ["string", "null"]},
            "by": {"type": "string", "minLength": 1},
            "command_id": {"type": "string", "minLength": 1},
        },
        "required": ["input_id", "lane", "text", "instance", "by", "command_id"],
        "additionalProperties": False,
    },
    writer="inputs",
    boundaries=frozenset({"U4"}),
    dedup_key="input:{input_id}",
)
INPUT_CONSUMED = EventTypeSpec(
    name="input.consumed",
    schema={
        "type": "object",
        "properties": {
            "input_id": _INPUT_ID,
            "by": {"enum": ["context", "stop_policy", "amend"]},
            "step_id": {"type": "string", "minLength": 1},
            "interrupt": {"type": "string", "minLength": 1},
        },
        "required": ["input_id", "by"],
        "additionalProperties": False,
    },
    writer="inputs",
    boundaries=frozenset({"U2", "U3", "U4"}),
)
INTERRUPT_AMENDED = EventTypeSpec(
    name="interrupt.amended",
    schema={
        "type": "object",
        "properties": {
            "interrupt": {"type": "string", "minLength": 1},
            "by": {"type": "string", "minLength": 1},
            "answers": {
                "type": "object",
                "additionalProperties": {"type": "string", "minLength": 1},
                "minProperties": 1,
            },
            "input_id": _INPUT_ID,
        },
        "required": ["interrupt", "by", "answers"],
        "additionalProperties": False,
    },
    writer="interrupts",
    boundaries=frozenset({"U4"}),
)

# --- Uploaded documents (the ingest extension's ADR, D2, D7) ---------------------------
# An upload that goes through ingestion records what each file is with the upload's source
# change (U8): ``document.stored`` once its bytes are in the blob store and typed,
# ``document.gap`` for a dead end the model is told about, ``document.refused`` for a cap
# that failed before anything was stored. The text itself is in the input workspace and the
# original in the blob store, so a payload holds facts only.

_DOC_KEY = {"type": "string", "pattern": "^D-[0-9]{4,}$"}
_NULLABLE_TEXT = {"type": ["string", "null"]}

DOCUMENT_STORED = EventTypeSpec(
    name="document.stored",
    schema={
        "type": "object",
        "properties": {
            "doc_key": _DOC_KEY,
            "version": {"type": "integer", "minimum": 1},
            "path": {"type": "string", "minLength": 1},
            "blob": _HASH,
            "name": {"type": "string"},
            "origin": {"enum": ["upload", "message", "tool_result", "attachment"]},
            "parent": _NULLABLE_TEXT,
            "declared_type": _NULLABLE_TEXT,
            "detected_type": {"type": "string", "minLength": 1},
            "charset": _NULLABLE_TEXT,
            "type_conflict": _NULLABLE_TEXT,
            "size_bytes": {"type": "integer", "minimum": 0},
            "representation": {
                "type": "object",
                "properties": {
                    "chars": {"type": "integer", "minimum": 0},
                    "anchors": {"type": "integer", "minimum": 0},
                    "converters": {"type": "array", "items": {"type": "string"}},
                    "dropped": {"type": "array", "items": {"type": "string"}},
                    "served": {"enum": ["as_given", "converted"]},
                },
                "required": ["chars", "anchors", "converters", "dropped", "served"],
                "additionalProperties": False,
            },
            "route": {
                "type": "object",
                "properties": {
                    "kind": {"enum": ["text", "native_file", "native_image", "header_only"]},
                    "reason": {"type": "string"},
                    "profile": {"type": "string"},
                    "sent": _HASH,
                    "width": {"type": "integer", "minimum": 1},
                    "height": {"type": "integer", "minimum": 1},
                    "tokens": {"type": "integer", "minimum": 0},
                    "passed_through": {"type": "boolean"},
                },
                "required": ["kind"],
                "additionalProperties": False,
            },
        },
        "required": [
            "doc_key",
            "version",
            "path",
            "blob",
            "name",
            "origin",
            "parent",
            "declared_type",
            "detected_type",
            "charset",
            "type_conflict",
            "size_bytes",
        ],
        "additionalProperties": False,
    },
    writer="inputs",
    boundaries=frozenset({"U8"}),
    dedup_key="document:{doc_key}/v{version}",
)
DOCUMENT_GAP = EventTypeSpec(
    name="document.gap",
    schema={
        "type": "object",
        "properties": {
            "doc_key": _DOC_KEY,
            "version": {"type": "integer", "minimum": 1},
            "path": {"type": "string", "minLength": 1},
            "reason": {"enum": ["refused", "locked", "unsupported", "unreadable", "cancelled"]},
            "detail": {"type": "string"},
            "steps": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["doc_key", "version", "path", "reason", "detail", "steps"],
        "additionalProperties": False,
    },
    writer="inputs",
    boundaries=frozenset({"U8"}),
)
DOCUMENT_REFUSED = EventTypeSpec(
    name="document.refused",
    schema={
        "type": "object",
        "properties": {
            "cap": {"type": "string", "minLength": 1},
            "value": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 0},
            "origin": {"enum": ["upload", "message", "tool_result", "attachment"]},
            "name": {"type": "string"},
            "path": {"type": "string", "minLength": 1},
        },
        "required": ["cap", "value", "limit", "origin", "name"],
        "additionalProperties": False,
    },
    writer="inputs",
    boundaries=frozenset({"U8"}),
)

CORE_EVENT_TYPES: tuple[EventTypeSpec, ...] = (
    RUN_STARTED,
    RUN_ENDED,
    ARTIFACT_SAVED,
    READ_RECORDED,
    RELATION_SAVED,
    EXCERPTS_MINTED,
    EXCERPTS_SHOWN,
    CITATION_BOUND,
    RECOVERY_COMPLETED,
    STEP_STARTED,
    STEP_DISPATCHED,
    STEP_FAILED,
    ATTEMPT_DISCARDED,
    STEP_COMPLETED,
    CONTEXT_COMPACTED,
    OUTBOX_ENQUEUED,
    OUTBOX_CLAIMED,
    OUTBOX_FAILED,
    OUTBOX_COMPLETED,
    OUTBOX_ABANDONED,
    COMMAND_ACCEPTED,
    COMMAND_REFUSED,
    STOP_DECIDED,
    BUDGET_REACHED,
    BUDGET_GRANTED,
    RUN_FINALIZING,
    RUN_STOP_REQUESTED,
    INTERRUPT_VOIDED,
    SOURCE_CHANGED,
    QUERY_REEXECUTED,
    APPROVAL_REVOKED,
    INTERRUPT_RAISED,
    INTERRUPT_RESOLVED,
    REVIEW_ACCEPTED,
    REVIEW_REJECTED,
    APPROVAL_REQUESTED,
    APPROVAL_GRANTED,
    APPROVAL_REJECTED,
    APPROVAL_VOIDED,
    TASK_ENTERED,
    TASK_WITHDRAWN,
    COMPLETION_CHECK_EVALUATED,
    COMPLETION_CHECK_RESET,
    KEYS_LISTED,
    USER_MESSAGE,
    ROUTE_DECIDED,
    TIMER_SCHEDULED,
    TIMER_FIRED,
    TIMER_CANCELLED,
    SOURCE_EXPIRED,
    INPUT_QUEUED,
    INPUT_CONSUMED,
    INTERRUPT_AMENDED,
    DOCUMENT_STORED,
    DOCUMENT_GAP,
    DOCUMENT_REFUSED,
)
"""The runtime core's closed catalogue. Each primitive adds its section when it is built."""


def _json_path(parts: Iterable[str | int]) -> str:
    return "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in parts)


def _template_fields(template: str) -> list[str]:
    return [field for _, field, _, _ in string.Formatter().parse(template) if field]


class Catalogue:
    """The event types one process may append: the core types plus registered extensions."""

    def __init__(self, specs: Iterable[EventTypeSpec]) -> None:
        """Build a catalogue of core types, checking each (see :meth:`register_extension`)."""
        self._specs: dict[str, EventTypeSpec] = {}
        self._validators: dict[str, Draft202012Validator] = {}
        self._extensions: set[str] = set()
        for spec in specs:
            self._add(spec)

    def _add(self, spec: EventTypeSpec) -> None:
        if spec.name in self._specs:
            raise CatalogueError(message=f"event type {spec.name} declared twice (already held)")
        namespace, _, verb = spec.name.partition(".")
        if not namespace or not verb:
            raise CatalogueError(message=f"{spec.name}: a name is <namespace>.<past participle>")
        if not spec.boundaries:
            raise CatalogueError(message=f"{spec.name}: names no transaction boundary")
        for b in spec.boundaries:
            boundary(b)
        prefix = spec.dedup_prefix
        if prefix is not None and (not prefix.endswith(":") or prefix == ":"):
            raise CatalogueError(message=f"{spec.name}: a dedup key starts with a prefix like 'x:'")
        schema = copy.deepcopy(dict(spec.schema))  # the caller's nested dicts stay theirs
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as e:
            raise CatalogueError(message=f"{spec.name}: invalid schema: {e.message}") from None
        self._specs[spec.name] = spec
        self._validators[spec.name] = Draft202012Validator(schema)

    def __contains__(self, name: object) -> bool:
        """Whether an event type of this name is held."""
        return name in self._specs

    def get(self, name: str) -> EventTypeSpec:
        """The spec of ``name``, or :class:`UnknownEventType`."""
        try:
            return self._specs[name]
        except KeyError:
            raise UnknownEventType(message=f"no event type {name!r} in the catalogue") from None

    def names(self) -> tuple[str, ...]:
        """Every held event type name, sorted."""
        return tuple(sorted(self._specs))

    def is_extension(self, name: str) -> bool:
        """Whether ``name`` was registered by an extension rather than declared by the core."""
        return name in self._extensions

    def core_namespaces(self) -> frozenset[str]:
        """The namespaces of the core types; an extension may use none of them."""
        return frozenset(s.namespace for n, s in self._specs.items() if n not in self._extensions)

    def core_dedup_prefixes(self) -> frozenset[str]:
        """The dedup-key prefixes of the core types; an extension may use none of them."""
        return frozenset(
            s.dedup_prefix
            for n, s in self._specs.items()
            if n not in self._extensions and s.dedup_prefix is not None
        )

    def extension_refusal(self, spec: EventTypeSpec, *, extension: str) -> str | None:
        """Why ``extension`` may not register ``spec``, or None. Pure: nothing is added.

        The name must carry the extension's namespace (``memory.recalled`` from ``memory``)
        and the extension must be its one writer. A name already held, a core namespace, a
        core dedup prefix, and anything a core type is refused for are refused too. The same
        spec registered again by the same extension is not a refusal (two agents may load
        one extension).
        """
        held = self._specs.get(spec.name)
        if held is not None:
            same = held == spec and spec.name in self._extensions
            return None if same else f"event type {spec.name} is already registered"
        if spec.namespace != extension:
            return f"{spec.name}: an event type of extension {extension!r} is named {extension}.*"
        if spec.writer != extension:
            return f"{spec.name}: its writer is {spec.writer!r}; the extension {extension!r} is"
        if spec.namespace in self.core_namespaces():
            return f"{spec.name}: {spec.namespace!r} is a core namespace"
        if spec.dedup_prefix is not None and spec.dedup_prefix in self.core_dedup_prefixes():
            return f"{spec.name}: {spec.dedup_prefix!r} is a core dedup prefix"
        try:
            Catalogue([spec])
        except (CatalogueError, AkshaError) as e:
            return e.message or e.code
        return None

    def register_extension(self, spec: EventTypeSpec, *, extension: str) -> None:
        """Add ``extension``'s namespaced event type (ADR-P01 D4); the agent loader calls it.

        Raises :class:`CatalogueError` for every case :meth:`extension_refusal` names. It
        uses the core's transaction boundaries: an extension adds event types, never
        boundaries. Registering the same spec twice from the same extension is a no-op.
        """
        problem = self.extension_refusal(spec, extension=extension)
        if problem is not None:
            raise CatalogueError(message=problem)
        if spec.name in self._specs:
            return
        self._add(spec)
        self._extensions.add(spec.name)

    def check_boundary(self, name: str, boundary_id: str) -> None:
        """Refuse ``name`` in ``boundary_id`` unless its spec allows that boundary."""
        boundary(boundary_id)
        if boundary_id not in self.get(name).boundaries:
            raise BoundaryNotAllowed(message=f"{name} may not be appended in {boundary_id}")

    def validate_payload(
        self, name: str, payload: Mapping[str, Any], *, cap: int | None = PAYLOAD_CAP_BYTES
    ) -> str:
        """Check ``payload`` against the cap and the schema of ``name``; return its canonical JSON.

        Raises :class:`PayloadTooLarge` over ``cap`` (:data:`PAYLOAD_CAP_BYTES`; None checks
        no size, which only the blob offload uses before it moves values out), and
        :class:`PayloadInvalid` naming the failing JSON path otherwise, including a string
        that holds NUL, which PostgreSQL cannot store (so both backends refuse it alike),
        and a value outside the hash chain's encoding (a float, an integer beyond plus or
        minus 2**53), which the chain could not hash the same way everywhere (ADR-P01 H3).
        A blob stub (:mod:`aksha.runtime.events.stubs`) stands for a value checked before
        it was moved to the blob store, so no schema error at or inside a stub counts.
        """
        self.get(name)
        try:
            text = canonical_json(payload)
        except (TypeError, ValueError) as e:
            raise PayloadInvalid(name, "$", str(e)) from None
        nul = nul_path(payload)
        if nul is not None:
            raise PayloadInvalid(name, nul, "a string holds NUL (\\u0000), which no backend stores")
        try:
            canonical_bytes(payload)
        except NotCanonical as e:
            raise PayloadInvalid(name, e.path, e.reason) from None
        size = len(text.encode("utf-8"))
        if cap is not None and size > cap:
            raise PayloadTooLarge(name, size, cap)
        stubs = stub_paths(payload)
        errors = self._validators[name].iter_errors(payload)
        error = next((e for e in errors if not _at_stub(e, stubs)), None)
        if error is not None:
            raise PayloadInvalid(name, _json_path(error.absolute_path), error.message)
        return text

    def dedup_fields(self, name: str) -> frozenset[str]:
        """The fields the dedup-key template of ``name`` reads (empty without a template)."""
        template = self.get(name).dedup_key
        return frozenset(_template_fields(template)) if template else frozenset()

    def render_dedup_key(
        self, name: str, envelope: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> str | None:
        """Render the dedup key of ``name`` from the envelope and the payload, or None.

        Envelope fields win over payload fields of the same name. A field that is absent
        or None raises :class:`DedupKeyError` with code ``dedup_key_field_missing``.
        """
        template = self.get(name).dedup_key
        if template is None:
            return None
        values = {**payload, **{k: v for k, v in envelope.items() if v is not None}}
        for field in _template_fields(template):
            if values.get(field) is None:
                raise DedupKeyError(
                    "dedup_key_field_missing", f"{name}: dedup key needs {field!r} ({template})"
                )
        return template.format_map(values)


def _at_stub(error: ValidationError, stubs: tuple[Path, ...]) -> bool:
    """Whether ``error`` is only about values a blob stub stands for.

    An error at or inside a stub is; so is a combinator's error (``anyOf``, ``oneOf``)
    whose every branch failed only at stubs.
    """
    if not stubs:
        return False
    if under(tuple(error.absolute_path), stubs):
        return True
    return bool(error.context) and all(_at_stub(e, stubs) for e in error.context)
