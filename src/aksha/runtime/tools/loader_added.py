"""The three tools the loader adds to every registry: ``finish``, ``ask``, ``save_artifact``.

ADR-P10 D8. No agent or extension declares them (their names are taken), and each still
appears in the permission matrix. Two words, kept apart on purpose:

1. *Loader-added* (:data:`LOADER_ADDED_TOOLS`): the three names above, whatever their
   effect class.
2. *Reserved* (the effect class ``"reserved"``): only ``finish`` and ``ask``. The runtime
   handles them itself, they have no body, and they are the only tools a stop-policy
   narrowing can never remove (ADR-P11 layer 4). Only :data:`RUNTIME_OWNER` may declare a
   tool with this effect class.

``finish`` is offered on every Task (the model proposes to stop; the stop policy decides),
``ask`` on a Task with a declared interrupt, ``save_artifact`` where a Task lists it.
``save_artifact`` is one generic typed save, a ``store`` tool whose body returns a single
``SaveRequest``; the step executor restricts that save to the Task's ``writes``, and a
narrowing (plan mode) removes it like any other store tool.

What the model is shown of ``save_artifact`` is made per Task (:func:`offered_for_task`):
its description names each kind the Task writes, whether it takes a ``key``, which fields
must cite their evidence, and the kind's content schema, so a model can fill the content
right on its first call; where a kind cites, it also spells out how a cite is written
(:func:`citation_contract`). The parameters schema stays the one the gate validates (ADR-P10
D3); the store checks the content against the kind's schema.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Any

from aksha.runtime.artifacts.types import (
    SELF,
    ArtifactKind,
    End,
    Excerpt,
    InputRef,
    RelationRequest,
    SaveRequest,
)
from aksha.runtime.canonical import thaw
from aksha.runtime.events.catalogue import INPUT_REF_SCHEMA
from aksha.runtime.grounding.types import MIN_QUOTE_CHARS, UNKNOWN_REASONS
from aksha.runtime.grounding.verify import UNKNOWN_EXAMPLE
from aksha.runtime.tools.types import Ok, ToolContext, ToolDecl, ToolOutcome, ToolSpec, ToolView

LOADER_ADDED_TOOLS: frozenset[str] = frozenset({"finish", "ask", "save_artifact"})
"""The tools the loader adds to every registry; no agent or extension may declare these names."""

IMPLICIT_TOOLS: frozenset[str] = frozenset({"finish", "ask"})
"""Reserved tools a Task holds without listing them (``ask`` only with an interrupt)."""

RUNTIME_OWNER = "runtime"
"""The owner of the loader-added tools, and the only declarer of a ``reserved`` tool."""


def _end(value: Any) -> End:
    return SELF if value == SELF else InputRef.from_json(value)


def _relation(value: Mapping[str, Any]) -> RelationRequest:
    return RelationRequest(
        kind=str(value["kind"]),
        src=_end(value["src"]),
        dst=_end(value["dst"]),
        evidence=tuple(Excerpt.from_json(x) for x in value.get("evidence", ())),
        label=value.get("label"),
    )


def save_artifact(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """Ask the store to save ``content`` as one ``kind`` version under ``key``.

    ``primary_inputs`` and ``relations`` are read from their JSON forms
    (``InputRef.as_json``, ``Excerpt.as_json``, an end being an input or ``"self"``).
    """
    kind, key = str(args["kind"]), args.get("key")
    request = SaveRequest(
        kind,
        None if key is None else str(key),
        args["content"],
        primary_inputs=tuple(InputRef.from_json(r) for r in args.get("primary_inputs", ())),
        relations=tuple(_relation(r) for r in args.get("relations", ())),
    )
    return ToolOutcome(Ok({"kind": kind, "key": request.key}), saves=(request,))


_FINISH = ToolSpec(
    name="finish",
    owner=RUNTIME_OWNER,
    effect="reserved",
    description="Propose that this Task is done. The completion check decides.",
    schema={
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "additionalProperties": False,
    },
)

_ASK = ToolSpec(
    name="ask",
    owner=RUNTIME_OWNER,
    effect="reserved",
    description="Ask a person a question and wait for the answer.",
    schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": 1},
            # ADR-P17 S2: several questions, each answered (and cited) on its own.
            "questions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "text": {"type": "string", "minLength": 1},
                    },
                    "required": ["id", "text"],
                    "additionalProperties": False,
                },
            },
            "options": {"type": "array", "items": {"type": "string"}},
        },
        "anyOf": [{"required": ["question"]}, {"required": ["questions"]}],
        "additionalProperties": False,
    },
)

_SAVE_ARTIFACT = ToolSpec(
    name="save_artifact",
    owner=RUNTIME_OWNER,
    effect="store",
    description="Save one artifact of a kind this Task writes.",
    schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "minLength": 1},
            "key": {"type": ["string", "null"]},
            "content": {"type": "object"},
            "primary_inputs": {"type": "array", "items": INPUT_REF_SCHEMA},
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "minLength": 1},
                        "src": {"anyOf": [{"const": SELF}, INPUT_REF_SCHEMA]},
                        "dst": {"anyOf": [{"const": SELF}, INPUT_REF_SCHEMA]},
                        "label": {"type": ["string", "null"]},
                        "evidence": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "source": INPUT_REF_SCHEMA,
                                    "quote": {"type": "string", "minLength": 1},
                                    "pointer": {"type": "string"},
                                    "span": {"type": "string", "minLength": 1},
                                },
                                "required": ["source", "quote"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["kind", "src", "dst", "evidence"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["kind", "content"],
        "additionalProperties": False,
    },
)

LOADER_ADDED_DECLS: Mapping[str, ToolDecl] = MappingProxyType(
    {
        "finish": ToolDecl(_FINISH, body=None),
        "ask": ToolDecl(_ASK, body=None),
        "save_artifact": ToolDecl(_SAVE_ARTIFACT, body=save_artifact),
    }
)
"""The three loader-added tools, by name, as the registry adds them."""


SAVE_TOOL = "save_artifact"

_JSON_TYPES: Mapping[str, str] = MappingProxyType(
    {"str": "string", "int": "integer", "float": "number", "bool": "boolean",
     "list": "array", "tuple": "array", "dict": "object"}
)  # fmt: skip


def _field_type(annotation: Any) -> dict[str, Any]:
    name = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    found = _JSON_TYPES.get(str(name).split("[", 1)[0].strip())
    return {"type": found} if found else {}


def content_schema(kind: ArtifactKind) -> dict[str, Any]:
    """The JSON Schema of ``kind``'s content: its own, or one made from a dataclass's fields."""
    schema = kind.schema
    if isinstance(schema, Mapping):
        return dict(thaw(schema))
    if not (dataclasses.is_dataclass(schema) and isinstance(schema, type)):
        return {"type": "object"}
    fields = dataclasses.fields(schema)
    return {
        "type": "object",
        "properties": {f.name: _field_type(f.type) for f in fields},
        "required": [
            f.name
            for f in fields
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        ],
        "additionalProperties": False,
    }


def _compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _kind_line(name: str, kind: ArtifactKind) -> str:
    key = "keyed: pass `key`" if kind.keyed else "not keyed: omit `key`"
    lines = [f'- kind "{name}" ({key}). content schema: {_compact(content_schema(kind))}']
    if kind.cites:
        cited = ", ".join(
            f"{c.field} ({' or '.join(sorted(c.forms))}"
            f"{', or an explicit unknown' if c.may_be_unknown else ''})"
            for c in kind.cites
        )
        lines.append(f"  Fields that cite their evidence: {cited}.")
    return "\n".join(lines)


def citation_contract(kinds: Mapping[str, ArtifactKind]) -> tuple[str, ...]:
    """How a cited field is written, for the cite forms and unknowns ``kinds`` allow (ADR-P13).

    Generic: it names no kind or field, only the shapes the verifier accepts, the source of
    span IDs, the verbatim and length rule, an explicit unknown, and one invented example.
    """
    policies = [c for k in kinds.values() for c in k.cites]
    if not policies:
        return ()
    forms = {f for c in policies for f in c.forms}
    lines = [
        "How to cite: a field that cites its evidence is a JSON object that holds a `cite` "
        "object, beside its other fields (such as `value`). The span ID and the quote go "
        "inside `cite`, never beside the value."
    ]
    if "span" in forms:
        lines.append(
            '- A span cite is {"cite": {"span": "EX-0042", "quote": "..."}}. Span IDs '
            "are the EX-#### IDs a tool result listed in `citation_spans` for the text it "
            "showed you; cite only those. The quote is copied verbatim from that span's "
            f"text, at least {MIN_QUOTE_CHARS} characters, or the whole span when it is "
            "shorter."
        )
    if "statement" in forms:
        lines.append(
            '- A statement cite is {"cite": {"statement": "EX-0007"}}: the ID of a '
            "person's answer."
        )
    if "artifact" in forms:
        lines.append(
            '- An artifact cite is {"cite": {"artifact": "<kind>/<key>@v1", "path": "/rows/0"}}'
            ": a version you read (an unkeyed kind is <kind>@v1) and a JSON pointer into it."
        )
    if any(c.value_in_quote for c in policies):
        lines.append("- Where a field's `value` must be in its quote, write it as the quote does.")
    if any(c.may_be_unknown for c in policies):
        lines.append(
            "- A field marked 'or an explicit unknown' that no source settles holds only "
            f"{UNKNOWN_EXAMPLE} (reason one of {', '.join(sorted(UNKNOWN_REASONS))}; "
            "not_found also lists the reads it checked in `checked`, unverified names the "
            "`failed_step`)."
        )
    if "span" in forms:
        lines.append(
            "- Example (invented): a result showed span EX-0042 with text "
            "'Starter plan: 12 EUR per seat per month.' A cited price is then "
            '{"value": "12 EUR per seat per month", "cite": {"span": "EX-0042", '
            '"quote": "12 EUR per seat per month"}}.'
        )
    return tuple(lines)


def save_description(kinds: Mapping[str, ArtifactKind]) -> str:
    """``save_artifact``'s description on a Task that writes ``kinds``, by full name."""
    if not kinds:
        return _SAVE_ARTIFACT.description
    return "\n".join(
        (
            "Save one artifact of a kind this Task writes. Pass `kind` exactly as named "
            "below and `content` as a JSON object that matches that kind's content schema; "
            "a refused save lists every field to fix. This Task writes:",
            *(_kind_line(n, kinds[n]) for n in sorted(kinds)),
            *citation_contract(kinds),
        )
    )


def offered_for_task(
    tools: Sequence[ToolView], kinds: Mapping[str, ArtifactKind]
) -> tuple[ToolView, ...]:
    """``tools`` with ``save_artifact`` described for a Task that writes ``kinds`` (full names).

    Only the description changes: the parameters schema is the validated one (ADR-P10 D3).
    """
    return tuple(
        replace(t, description=save_description(kinds)) if t.name == SAVE_TOOL else t for t in tools
    )
