"""The two kinds a supervisor's ``commit_round`` writes: one ``directive`` per item, one ``round``.

Both are ``save="tool_only"``: only ``commit_round`` writes them, in one step, so one
round's notes, plan and directives commit in one transaction or not at all. An agent
declares them in its own namespace (``kinds()``), like any kind it owns.

A directive is a decision, like a person's approval: nothing marks it stale. It records
the version of the item's triage output it was based on (``basis``), and the board shows
``basis_changed`` when that version has moved, so the supervisor re-decides explicitly.
No worker reads a directive: only the deep tier's key filter does, so re-deciding an item
re-runs nothing that already ran.
"""

from __future__ import annotations

from typing import Any

from aksha.runtime.api import ArtifactKind

from .spec import MAX_ENTRIES, MAX_QUESTIONS, NOTES_CHARS, PLAN_CHARS, REASON_CHARS

STATES = ("admitted", "dropped", "deferred")
"""What a directive decides for an item at the deep tier."""

DIRECTIVE = "directive"
ROUND = "round"

_TEXT = {"type": "string"}
_KEYS = {"type": "array", "items": _TEXT}

DIRECTIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["key", "state", "tier", "priority", "reason", "basis", "round"],
    "properties": {
        "key": _TEXT,
        "state": {"enum": list(STATES)},
        "tier": {"type": ["string", "null"]},
        "priority": {"type": "integer"},
        "reason": {"type": "string", "maxLength": REASON_CHARS},
        "basis": {"type": ["string", "null"]},
        "round": {"type": "integer", "minimum": 1},
    },
}

NOTES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["questions", "entries", "hypotheses"],
    "properties": {
        "questions": {"type": "array", "items": _TEXT, "maxItems": MAX_QUESTIONS},
        "entries": {
            "type": "array",
            "maxItems": MAX_ENTRIES,
            "items": {
                "type": "object",
                "required": ["keys", "text"],
                "properties": {"keys": _KEYS, "text": {"type": "string", "maxLength": 300}},
            },
        },
        "hypotheses": {"type": "string", "maxLength": NOTES_CHARS},
    },
}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["goal", "tier_rule", "budget_split"],
    "properties": {
        "goal": {"type": "string", "maxLength": PLAN_CHARS},
        "tier_rule": {"type": "string", "maxLength": PLAN_CHARS},
        "budget_split": {"type": "string", "maxLength": PLAN_CHARS},
    },
}

ROUND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["round", "notes", "plan", "admitted", "dropped", "deferred", "audit"],
    "properties": {
        "round": {"type": "integer", "minimum": 1},
        "notes": NOTES_SCHEMA,
        "plan": PLAN_SCHEMA,
        "admitted": _KEYS,
        "dropped": _KEYS,
        "deferred": _KEYS,
        "audit": _KEYS,
        "changed": _KEYS,
    },
}


def kinds() -> tuple[ArtifactKind, ...]:
    """The directive and round kinds, to declare in the agent's own namespace."""
    return (
        ArtifactKind(DIRECTIVE, DIRECTIVE_SCHEMA, keyed=True, save="tool_only"),
        ArtifactKind(ROUND, ROUND_SCHEMA, save="tool_only"),
    )
