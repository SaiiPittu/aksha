"""A read tool's artifact reader through the agent loop (ADR-P09 D3).

A tool that declares ``reads_kinds`` is given ``ToolContext.artifacts``, the owner's
versions of those kinds as the log stood before its step started; a tool that declares
none is given nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import Ok, ToolContext, ToolDecl, ToolOutcome, ToolSpec
from tests.support.loop import AGENT, SAVE_NOTE, Bench, Recorder, call, finish_reply, tools_reply


def _bench(tmp: Path, model: Recorder, seen: list[Any]) -> Bench:
    def peek(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        found = None if ctx.artifacts is None else ctx.artifacts.latest(f"{AGENT}.note")
        seen.append(found)
        return ToolOutcome(Ok({"seen": 0 if found is None else len(found)}))

    b = Bench(tmp / "aksha.sqlite", model)
    specs = (
        ToolSpec(
            "peek", AGENT, "read", "Peek.", {"type": "object"}, reads_kinds=frozenset({"note"})
        ),
        ToolSpec("blind", AGENT, "read", "Blind.", {"type": "object"}),
    )
    decls = {s.name: ToolDecl(s, peek) for s in specs}
    edit = replace(b.agent.tasks[0], tools=(*b.agent.tasks[0].tools, "peek", "blind"))
    b.agent = replace(b.agent, tools=(*b.agent.tools, *decls.values()), tasks=(edit,))
    listed = {t.spec.name: t for t in b.agent.tools if isinstance(t, ToolDecl)}
    b.setup = replace(
        b.setup, agent=b.agent, tree=compile_tree(AGENT, b.agent.tasks),
        registry=ToolRegistry(b.agent, listed, ()),
    )  # fmt: skip
    b.start()
    return b


def test_a_tool_that_declares_reads_kinds_reads_versions_as_of_its_step(tmp_path: Path) -> None:
    seen: list[Any] = []
    model = Recorder(
        [
            tools_reply(call("peek")),
            tools_reply(SAVE_NOTE, call("peek", 2)),
            tools_reply(call("blind")),
            finish_reply(),
        ]
    )
    b = _bench(tmp_path, model, seen)
    assert b.drive().outcome == "complete"
    first, second, blind = seen
    assert first == ()
    assert [str(s.ref) for s in second] == [f"{AGENT}.note@v1"]
    assert second[0].freshness == "current" and second[0].is_latest
    assert blind is None
    b.close()
