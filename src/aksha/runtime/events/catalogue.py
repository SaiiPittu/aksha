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
            "cause": {"enum": ["request", "rebuild", "handoff"]},
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
            # ADR-P05 S1, D8: a rebuild run names the runs whose invalidated work it
            # regenerates, and its key (one rebuild run per agent and source version).
            "for_runs": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "rebuild_key": {"type": "string", "pattern": "^rebuild:"},
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
    boundaries=frozenset({"U1", "U9"}),
    dedup_key="rstart:{run}",
)
RUN_ENDED = EventTypeSpec(
    name="run.ended",
    schema={
        "type": "object",
        "properties": {
            # ADR-P18 D2: ``stopped`` is a person's stop, never ``partial``.
            "outcome": {"enum": ["complete", "partial", "failed", "stopped"]},
            "reason": {"type": "string"},
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
        "kind": {"enum": ["record", "query", "statement", "artifact", "blob"]},
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
            "evidence": {"type": "array", "items": _EXCERPT, "minItems": 1},
            "carrier": {"type": "string", "minLength": 1},
            "n": {"type": "integer", "minimum": 0},
        },
        "required": ["kind", "label", "src", "dst", "evidence", "carrier", "n"],
        "additionalProperties": False,
    },
    writer="artifacts",
    boundaries=frozenset({"U2", "U10"}),
    dedup_key="rel:{carrier}#{n}",
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

# --- P14 budgets, and the part of P16's stop decision record P02 and P14 read ----------
# ``stop.decided`` is declared with the fields the budget and limit reducers read
# (``budgets/records.py``): a CONTINUE charges a turn, a ``cause: resume`` record is a
# resume record (recovery writes it, ADR-P02 D7), a ``candidate: finish`` CONTINUE is a
# veto. ADR-P16 widens the schema when it is built. ``budget.reached`` commits with the
# budget interrupt in the stop decision; ``budget.granted`` is a person's command.

_DIMENSION = {"enum": ["model_turns", "tool_calls", "active_ms", "tokens", "cost_micro_usd"]}

STOP_DECIDED = EventTypeSpec(
    name="stop.decided",
    schema={
        "type": "object",
        "properties": {
            "walk": {"type": "integer", "minimum": 0},
            "verdict": {"enum": ["continue", "hold", "stop"]},
            "cause": {"type": "string", "pattern": "^[a-z][a-z0-9_:]*$"},
            "candidate": {"type": "string", "minLength": 1},
            "turn": {"type": "integer", "minimum": 1},
            "resumes_turn": {"type": "boolean"},
            "reason": {"type": "string"},
            "limit": {"type": "string"},
            "counts": {"type": "object", "additionalProperties": {"type": "integer"}},
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
        },
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
# request an acceptance makes), and P17 tightens their schemas when it is built.

_REF_TEXT = {"type": "string", "minLength": 1}
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
            "kind": {"enum": ["input", "choice", "approval", "review"]},
            # ADR-P05: a diff review's card, and the choice a rejected review raises (S4).
            "old": _REF_TEXT,
            "revision": _REF_TEXT,
            "card_hash": _HASH,
            "changed_reads": {"type": "array", "items": _READ_STATUS},
            "via": {"type": "array", "items": {"type": "string"}},
            "hunks": {"type": "array", "items": _HUNK},
            "writer": {"type": "object"},
            "reason": _SNAKE,
        },
        "required": ["interrupt"],
        "allOf": [
            {
                "if": {"properties": {"kind": {"const": "review"}}, "required": ["kind"]},
                "then": {"required": ["old", "revision", "card_hash", "hunks"]},
            }
        ],
    },
    writer="interrupts",
    boundaries=frozenset({"U2", "U4"}),
)
INTERRUPT_RESOLVED = EventTypeSpec(
    name="interrupt.resolved",
    schema={
        "type": "object",
        "properties": {"interrupt": {"type": "string", "minLength": 1}},
        "required": ["interrupt"],
    },
    writer="session",
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
        },
        "required": ["approval"],
    },
    writer="interrupts",
    boundaries=frozenset({"U2", "U4"}),
)

CORE_EVENT_TYPES: tuple[EventTypeSpec, ...] = (
    RUN_STARTED,
    RUN_ENDED,
    ARTIFACT_SAVED,
    READ_RECORDED,
    RELATION_SAVED,
    RECOVERY_COMPLETED,
    STEP_STARTED,
    STEP_DISPATCHED,
    STEP_FAILED,
    ATTEMPT_DISCARDED,
    STEP_COMPLETED,
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
