"""The record reader through the agent loop (ADR-P13 D1): a tool body's span drafts are
minted only over texts the runtime's reader returned in that attempt, the model is shown
the span IDs it may cite, and a person's answer names its statement span."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from aksha.adapters.sources.folder import FolderSource
from aksha.runtime.context.types import CITE_AS
from aksha.runtime.grounding.types import SpanDraft
from aksha.runtime.interrupts.commands import answer
from aksha.runtime.interrupts.reducers import open_interrupts
from aksha.runtime.models.types import GenerationRequest
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import Err, Ok, ToolContext, ToolDecl, ToolOutcome, ToolSpec
from tests.support.loop import AGENT, OWNER, RUN, Bench, Recorder, call, finish_reply, tools_reply

RECORD = {"type": "object", "properties": {"record": {"type": "string"}}, "required": ["record"]}


def _fetch(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    found = ctx.reader.read("pages", str(args["record"])) if ctx.reader else None
    if found is None:
        return ToolOutcome(Err("fetch_failed", "no such page"))
    draft = SpanDraft(found.source, found.record_id, found.version, 0, len(found.text))
    return ToolOutcome(Ok({"text": found.text}, spans=(draft,)))


def _forge(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """Drafts a span over a text it never read through the reader."""
    return ToolOutcome(Ok({"text": "x"}, spans=(SpanDraft("pages", "p/a.txt", "v-made-up", 0, 1),)))


def _bench(tmp: Path, model: Recorder, *, reader: bool = True) -> Bench:
    (tmp / "p").mkdir()
    (tmp / "p" / "a.txt").write_text("Pricing: 279 EUR per sensor, billed yearly.", "utf-8")
    b = Bench(tmp / "aksha.sqlite", model)
    specs = [
        ToolSpec(n, AGENT, "read", n, RECORD, mints_citation_spans=True) for n in ("fetch", "forge")
    ]
    decls = {s.name: ToolDecl(s, body) for s, body in zip(specs, (_fetch, _forge), strict=True)}
    edit = replace(
        b.agent.tasks[0], tools=(*b.agent.tasks[0].tools, "fetch", "forge"), extracts=True
    )
    b.agent = replace(b.agent, tools=(*b.agent.tools, *decls.values()), tasks=(edit,))
    listed = {t.spec.name: t for t in b.agent.tools if isinstance(t, ToolDecl)}
    b.setup = replace(
        b.setup, agent=b.agent, tree=compile_tree(AGENT, b.agent.tasks),
        registry=ToolRegistry(b.agent, listed, ()),
        reader=FolderSource("pages", tmp) if reader else None,
    )  # fmt: skip
    b.start()
    return b


def _tool_texts(request: GenerationRequest) -> list[str]:
    return [m.text or "" for m in request.messages if m.role == "tool"]


def test_a_read_through_the_reader_mints_and_the_model_is_shown_the_span(tmp_path: Path) -> None:
    model = Recorder([tools_reply(call("fetch", record="p/a.txt")), finish_reply()])
    b = _bench(tmp_path, model)
    assert b.drive().outcome == "complete"
    (minted,) = b.events("excerpts.minted")
    (span,) = minted.payload["spans"]
    assert span["text"] == "Pricing: 279 EUR per sensor, billed yearly."
    assert span["version"].startswith("sha256:")
    assert list(minted.payload["shown"]) == [span["span_id"]]
    (shown,) = _tool_texts(model.requests[1])
    tail = json.loads(shown.rsplit("\n", 1)[1])
    assert tail["citation_spans"] == [span["span_id"]]
    assert tail["cite_as"] == CITE_AS  # how a saved field cites one of them
    b.close()


def test_a_draft_over_text_the_reader_never_returned_is_refused(tmp_path: Path) -> None:
    model = Recorder([tools_reply(call("forge", record="p/a.txt")), finish_reply()])
    b = _bench(tmp_path, model)
    b.drive()
    assert b.events("excerpts.minted") == []
    (forged,) = [e for e in b.events("step.completed") if e.payload.get("tool") == "forge"]
    assert forged.payload["code"] == "class_violation"
    b.close()


def test_without_a_reader_nothing_is_read_and_nothing_minted(tmp_path: Path) -> None:
    model = Recorder([tools_reply(call("fetch", record="p/a.txt")), finish_reply()])
    b = _bench(tmp_path, model, reader=False)
    b.drive()
    assert b.events("excerpts.minted") == []
    (fetched,) = [e for e in b.events("step.completed") if e.payload.get("tool") == "fetch"]
    assert fetched.payload["code"] == "fetch_failed"
    b.close()


def test_an_answer_names_its_statement_span_in_the_next_context(tmp_path: Path) -> None:
    ask = call("ask", questions=[{"id": "where", "text": "Which region?"}])
    model = Recorder([tools_reply(ask), finish_reply()])
    b = _bench(tmp_path, model)
    assert b.drive().state == "held"
    (i,) = open_interrupts(b.view(), RUN)
    assert answer(b.store, OWNER, i.id, by="p", answers={"where": "EU"}).refused is None
    b.drive()
    (span,) = [s for e in b.events("excerpts.minted") for s in e.payload["spans"]]
    said = [
        m.text or "" for m in model.requests[1].messages if (m.text or "").startswith("Answers")
    ]
    assert said == [
        "Answers from the person:\nQ: Which region?\nA: EU\n"
        f"(cite this answer as statement {span['span_id']})"
    ]
    b.close()


def test_a_tool_body_is_given_the_runs_recorded_start_date(tmp_path: Path) -> None:
    seen: list[str | None] = []

    def dated(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        seen.append(ctx.run_date)
        return _fetch(ctx, args)

    model = Recorder([tools_reply(call("fetch", record="p/a.txt")), finish_reply()])
    b = _bench(tmp_path, model)
    decl = b.setup.registry.get("fetch")
    listed = {t.spec.name: t for t in b.agent.tools if isinstance(t, ToolDecl)}
    listed["fetch"] = ToolDecl(decl.spec, dated)
    b.setup = replace(b.setup, registry=ToolRegistry(b.agent, listed, ()))
    assert b.drive().outcome == "complete"
    (started,) = b.events("run.started")
    assert seen == [started.at[:10]] and len(started.at) > 10
    b.close()
