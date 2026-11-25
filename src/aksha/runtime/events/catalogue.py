"""The event type catalogue: ``EventTypeSpec``, ``Catalogue`` and the core event types.

Each event type has a JSON Schema, one writer, the transaction boundaries it may appear
in and an optional dedup-key template (ADR-P01 D4, D5). Payloads are validated here on
append, and the 64 KiB payload cap (D7) is checked here.

How the catalogue grows:

1. A core primitive adds its event types to ``CORE_EVENT_TYPES`` when it is built, in a
   section headed by its ADR, using the D8 names (``stop.decided``, ``interrupt.raised``,
   ``outbox.enqueued``, ``task.entered``). P01 itself declares only the four types the
   event store and recovery need; ``step.started`` and ``step.completed`` (P02's) are
   declared in the shapes the policy gate records, ahead of the step executor.
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
from jsonschema.exceptions import SchemaError

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
            "outcome": {"enum": ["complete", "partial", "failed"]},
            "reason": {"type": "string"},
        },
        "required": ["outcome", "reason"],
    },
    writer="stop_policy",
    boundaries=frozenset({"U3"}),
    dedup_key="rend:{run}",
)
ARTIFACT_SAVED = EventTypeSpec(
    name="artifact.saved",
    schema={
        "type": "object",
        "properties": {
            "ref": {"type": "string", "minLength": 1},
            "kind": {"type": "string"},
            "key": {"type": ["string", "null"]},
            "version": {"type": "integer", "minimum": 1},
            "content_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        },
        "required": ["ref", "kind", "key", "version", "content_hash"],
    },
    writer="artifacts",
    boundaries=frozenset({"U2", "U10"}),
    dedup_key="save:{ref}",
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

# --- P02 step facts, in the shapes the policy gate (P11) records ------------------------
# P11 D9: an allow is the ``step.started`` that follows; a refusal is one
# ``step.completed{outcome: refused, code, layer}``. The durable step executor (P02) owns
# both types and adds its own fields (semantics, results) when it is built.

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

STEP_STARTED = EventTypeSpec(
    name="step.started",
    schema={
        "type": "object",
        "properties": {
            "kind": {"enum": ["tool", "model"]},
            "tool": {"type": "string", "minLength": 1},
            "args_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
            "position": {"type": "integer", "minimum": 0},
            "route": {"enum": ["dispatch", "outbox", "approval", "model", "reserved"]},
            "counted": {"type": "boolean"},
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
            "outcome": {"enum": ["ok", "error", "refused", "interrupted", "outcome_unknown"]},
            "tool": {"type": "string", "minLength": 1},
        },
        "required": ["outcome"],
        "allOf": [_REFUSED],
    },
    writer="execution",
    boundaries=frozenset({"U2"}),
    dedup_key="sdone:{step}",
)

CORE_EVENT_TYPES: tuple[EventTypeSpec, ...] = (
    RUN_STARTED,
    RUN_ENDED,
    ARTIFACT_SAVED,
    RECOVERY_COMPLETED,
    STEP_STARTED,
    STEP_COMPLETED,
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

    def validate_payload(self, name: str, payload: Mapping[str, Any]) -> str:
        """Check ``payload`` against the cap and the schema of ``name``; return its canonical JSON.

        Raises :class:`PayloadTooLarge` over :data:`PAYLOAD_CAP_BYTES`, and
        :class:`PayloadInvalid` naming the failing JSON path otherwise, including a string
        that holds NUL, which PostgreSQL cannot store (so both backends refuse it alike),
        and a value outside the hash chain's encoding (a float, an integer beyond plus or
        minus 2**53), which the chain could not hash the same way everywhere (ADR-P01 H3).
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
        if size > PAYLOAD_CAP_BYTES:
            raise PayloadTooLarge(name, size, PAYLOAD_CAP_BYTES)
        error = next(iter(self._validators[name].iter_errors(payload)), None)
        if error is not None:
            raise PayloadInvalid(name, _json_path(error.absolute_path), error.message)
        return text

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
