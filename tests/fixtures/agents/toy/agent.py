"""The conformance agent's definition: declarations only, checks in ``checks.py``."""

from __future__ import annotations

from aksha.runtime.api import (
    Agent,
    ArtifactKind,
    Budget,
    ContextPolicy,
    Goal,
    Limits,
    ModelAlias,
    Read,
    RelationKind,
    Task,
    TodoReminder,
    ToolUse,
    source_keys,
)

from . import checks

LINE = {
    "type": "object",
    "properties": {"line": {"type": "string", "minLength": 1}},
    "required": ["line"],
}
SUMMARY = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}

AGENT = Agent(
    name="toy",
    purpose="Summarise a few short notes, one line each, then one overall line.",
    instructions="prompts/instructions.md",
    reads=("record:notes",),
    artifacts=(ArtifactKind("line", LINE, keyed=True), ArtifactKind("summary", SUMMARY)),
    relations=(RelationKind("summarises", src=("summary",), dst=("line",)),),
    tools=(ToolUse("echo.lookup"), ToolUse("save_artifact")),
    tasks=(
        Task(
            "collect",
            per=source_keys("notes"),
            extracts=True,
            tools=("echo.lookup", "save_artifact"),
            writes=("line",),
            stop_policies=(TodoReminder(every=2),),
            children=(
                Task("read", tools=("echo.lookup",), done=checks.note_read),
                Task(
                    "write",
                    tools=("save_artifact",),
                    done=checks.line_saved,
                    model_aliases=("classify.tone",),
                ),
            ),
        ),
        Task(
            "summarise",
            needs=("collect",),
            reads=(Read("toy.line", keys="all"),),
            tools=("save_artifact",),
            writes=("summary",),
            done=checks.summary_saved,
            brief="prompts/summarise.md",
            stop_policies=(Goal(checks.every_line_in_summary),),
        ),
    ),
    completion=(checks.summary_saved,),
    model_aliases=(ModelAlias("classify.tone", "classification"),),
    budget=Budget(model_turns=10, tool_calls=20),
    limits=Limits(turn_limit=5),
    context_policy=ContextPolicy(result_cap_chars=2_000),
    extensions=("echo",),
)
