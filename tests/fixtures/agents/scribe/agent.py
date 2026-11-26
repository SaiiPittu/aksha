"""The scribe: one Task that writes a note, with a question and an approved publish.

Its publish delivers to the board extension's system of record, ``board.ledger``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.api import (
    Agent,
    ApprovalRule,
    ArtifactKind,
    Budget,
    DeclaredInterrupt,
    EffectSpec,
    Ok,
    Task,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
    ToolUse,
)

from . import checks

NOTE = {
    "type": "object",
    "properties": {"text": {"type": "string", "minLength": 1}},
    "required": ["text"],
}
POST = {
    "type": "object",
    "properties": {"text": {"type": "string", "minLength": 1}},
    "required": ["text"],
    "additionalProperties": False,
}


def _facts(view: object, args: Mapping[str, object]) -> Mapping[str, str]:
    return {"text": str(args["text"])}


def _never(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """An external tool's effect runs through the outbox, never through this body."""
    return ToolOutcome(Ok({"posted": False}))


PUBLISH = ToolSpec(
    "publish",
    "scribe",
    "external",
    "Publish a note to the board.",
    POST,
    external=EffectSpec("board.ledger", ("/text",), "reconcile"),
    approval=ApprovalRule(facts=_facts),
)

AGENT = Agent(
    name="scribe",
    purpose="Write one short note for a request.",
    instructions="prompts/instructions.md",
    reads=(),
    artifacts=(ArtifactKind("note", NOTE),),
    tools=(ToolDecl(PUBLISH, _never), ToolUse("save_artifact")),
    tasks=(
        Task(
            "write",
            tools=("publish", "save_artifact"),
            writes=("note",),
            done=checks.note_saved,
            interrupt=DeclaredInterrupt("input", "needs_detail"),
        ),
    ),
    budget=Budget(model_turns=10, tool_calls=20),
    extensions=("board",),
)
