"""Common tools any agent may list: a plan list (``todo``) and a scratchpad (``think``).

ADR-P10 amendment T2. Neither has an effect outside the call, so both are ``read`` tools
with no collection, and neither mints a span.

``todo(todos)`` takes the whole plan each call, every item ``{content, status}`` with
``status`` one of ``pending``, ``in_progress`` and ``done``, and at most one item in
progress. The plan is not stored anywhere else: the call and its result are on the event
log, so the latest call is the plan (store facts, derive interpretations), and a rebuilt
context shows it like any result. A list with two items in progress, or an item named
twice, is refused with the items named, so the model corrects it.

``think(thought)`` records a thought and returns nothing new. It is off by default (no
agent lists it): a model with its own reasoning gains little from it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.tools.types import Err, Ok, ToolContext, ToolDecl, ToolOutcome, ToolSpec

TODO = "todo"
THINK = "think"
TODO_STATUSES = ("pending", "in_progress", "done")
MAX_TODOS = 50

TODO_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "maxItems": MAX_TODOS,
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "minLength": 1, "maxLength": 300},
                    "status": {"enum": list(TODO_STATUSES)},
                },
                "required": ["content", "status"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["todos"],
    "additionalProperties": False,
}
THINK_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {"thought": {"type": "string", "minLength": 1, "maxLength": 8_000}},
    "required": ["thought"],
    "additionalProperties": False,
}


def todo(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """The plan, checked and echoed back with its counts (module doc)."""
    items = [
        {"content": " ".join(str(t["content"]).split()), "status": str(t["status"])}
        for t in args["todos"]
    ]
    doing = [n for n, t in enumerate(items) if t["status"] == "in_progress"]
    if len(doing) > 1:
        return ToolOutcome(
            Err(
                "todo_invalid",
                f"items {doing} are all in progress; keep one in "
                "progress at a time and send the whole list again",
                paths=tuple(f"/todos/{n}/status" for n in doing),
            )
        )
    seen: dict[str, int] = {}
    for n, t in enumerate(items):
        key = t["content"].casefold()
        if key in seen:
            return ToolOutcome(
                Err(
                    "todo_invalid",
                    f"items {seen[key]} and {n} are the same item; name each once",
                    paths=(f"/todos/{n}/content",),
                )
            )
        seen[key] = n
    counts = {s: sum(1 for t in items if t["status"] == s) for s in TODO_STATUSES}
    data = {
        "todos": items,
        "counts": counts,
        "note": "this is the plan now; send the whole list each time it changes",
    }
    return ToolOutcome(Ok(data))


def think(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """Nothing but an acknowledgement: the thought is on the log as the call's argument."""
    return ToolOutcome(Ok({"noted": True}))


def todo_tool(agent: str) -> ToolDecl:
    """``todo`` for ``agent`` (module doc)."""
    spec = ToolSpec(
        TODO,
        agent,
        "read",
        "Keep your plan for this task: send the whole list each time, each item with its "
        "status (pending, in_progress, done) and at most one in progress. Update it as you "
        "go, alongside your other calls, never as a turn on its own.",
        TODO_SCHEMA,
    )
    return ToolDecl(spec, todo)


def think_tool(agent: str) -> ToolDecl:
    """``think`` for ``agent`` (module doc); off unless the agent lists it."""
    spec = ToolSpec(
        THINK,
        agent,
        "read",
        "Write down a thought: what you know, what is left, what to do next. It changes "
        "nothing and returns nothing new.",
        THINK_SCHEMA,
    )
    return ToolDecl(spec, think)


__all__ = ["THINK", "TODO", "think_tool", "todo_tool"]
