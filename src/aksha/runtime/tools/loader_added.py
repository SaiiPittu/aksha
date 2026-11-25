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
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from aksha.runtime.artifacts.types import (
    SELF,
    End,
    Excerpt,
    InputRef,
    RelationRequest,
    SaveRequest,
)
from aksha.runtime.events.catalogue import INPUT_REF_SCHEMA
from aksha.runtime.tools.types import Ok, ToolContext, ToolDecl, ToolOutcome, ToolSpec

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
