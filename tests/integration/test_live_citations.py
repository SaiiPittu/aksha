"""A live model is told how to cite, recovers from a wrong cite in one retry, and gets the
turns a live run is sized for (ADR-P13 amendment L1, ADR-P14 amendment L1).

A real model that is never told where a cite's span ID and quote go writes them beside the
value, or quotes three words, and spends its Task's turns on refusals that name only the
first error. These tests pin the fix through the real agent loop with a stub model:

1. the save tool's description spells out the cite object, where span IDs come from, the
   verbatim and length rule and an explicit unknown, and a read result says the same
   beside its span IDs;
2. a save whose cites are wrong in two ways is refused once with both, each naming the rule
   it broke and the text of the span it named, and the next save, fixed, completes the Task;
3. a run whose ``run.started`` records a turn factor lets a Task take that many times its
   turn limit; a run that records none fails at the definition's own limit.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.sources.folder import FolderSource
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactKind, InputRef
from aksha.runtime.definitions.types import Agent
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.grounding.types import CitePolicy, SpanDraft, maybe_unknown
from aksha.runtime.loop.drive import drive
from aksha.runtime.loop.types import DriveResult, LoopSetup
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.types import GenerationRequest, GenerationResult
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import Task
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import Ok, ToolContext, ToolDecl, ToolOutcome, ToolSpec, ToolUse
from tests.support.events import START
from tests.support.loop import AGENT as CODER
from tests.support.loop import FINISH, Bench, Recorder, call, tools_reply
from tests.support.tasks import has

OWNER = "o1"
AGENT = "cited"
SOURCE = "docs"
RUN = "R-0001"
DEFINITION = "sha256:" + "c" * 64

PAGE = "SYN-Vendor sells the SYN-Sensor.\n\nThe SYN-Sensor costs 279 EUR per unit, billed once."
NAME, PRICE = PAGE.split("\n\n")
PATH = {
    "type": "object",
    "properties": {"path": {"type": "string", "minLength": 1}},
    "required": ["path"],
    "additionalProperties": False,
}
FIELD = {"type": "object"}
OFFER_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {"seller": FIELD, "price": FIELD, "warranty": maybe_unknown(FIELD)},
    "required": ["seller", "price", "warranty"],
}
OFFER = ArtifactKind(
    "offer",
    OFFER_SCHEMA,
    cites=(
        CitePolicy("/seller"),
        CitePolicy("/price"),
        CitePolicy("/warranty", may_be_unknown=True),
    ),
)
KINDS = {f"{AGENT}.offer": OFFER}
UNKNOWN = {"unknown": {"question": "What warranty does the seller give?", "reason": "not_checked"}}


def read_page(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """A read tool: the page's two paragraphs, one span each."""
    assert ctx.reader is not None
    found = ctx.reader.read(SOURCE, str(args["path"]))
    assert found is not None
    cut = found.text.index("\n\n")
    spans = (
        SpanDraft(SOURCE, found.record_id, found.version, 0, cut),
        SpanDraft(SOURCE, found.record_id, found.version, cut + 2, len(found.text)),
    )
    data = {"paragraphs": [found.text[:cut], found.text[cut + 2 :]]}
    read = InputRef("record", found.record_id, found.version)
    return ToolOutcome(Ok(data, spans=spans, reads=(read,)))


def _agent() -> Agent:
    spec = ToolSpec("read_page", AGENT, "read", "Read a page.", PATH,
                    collections=frozenset({"pages"}), mints_citation_spans=True)  # fmt: skip
    return Agent(
        name=AGENT,
        purpose="Record one synthetic offer.",
        instructions="prompts/instructions.md",
        reads=("record:pages",),
        artifacts=(OFFER,),
        tools=(ToolDecl(spec, read_page), ToolUse("save_artifact")),
        tasks=(
            Task(
                "offer",
                extracts=True,
                tools=("read_page", "save_artifact"),
                writes=("offer",),
                done=has("offer"),
                turn_limit=3,
            ),
        ),
    )


def _run(tmp: Path, model: Recorder) -> DriveResult:
    (tmp / "pages").mkdir()
    (tmp / "pages" / "p1.md").write_text(PAGE, encoding="utf-8")
    clock = FakeClock(START)
    store = open_store(tmp / "aksha.sqlite", clock, writer=True)
    agent = _agent()
    listed = {t.spec.name: t for t in agent.tools if isinstance(t, ToolDecl)}
    setup = LoopSetup(
        store=store, owner=OWNER, agent=agent, tree=compile_tree(AGENT, agent.tasks),
        registry=ToolRegistry(agent, listed, ()),
        table=bind_aliases((), "fake", {"generate": model}),
        instructions="Record the offer.", request="Record the offer.",
        reader=FolderSource(SOURCE, tmp), artifacts=ArtifactStore(KINDS), kinds=KINDS,
        definition_hash=DEFINITION,
    )  # fmt: skip
    started = {"agent": AGENT, "cause": "request", "definition_hash": DEFINITION}
    store.append(OWNER, "U1", (NewEvent("run.started", started, run=RUN),))
    try:
        return drive(setup, StepExecutor(store, OWNER, clock), RUN)
    finally:
        store.close()


def _tool_texts(request: GenerationRequest) -> list[str]:
    return [m.text or "" for m in request.messages if m.role == "tool"]


def _spans(request: GenerationRequest) -> list[str]:
    """The span IDs the read result listed, from the line after the result."""
    (read, *_) = _tool_texts(request)
    return list(json.loads(read.partition("\n")[2])["citation_spans"])


@pytest.fixture
def model() -> Iterator[Recorder]:
    def answer(request: GenerationRequest) -> GenerationResult:
        texts = _tool_texts(request)
        if not texts:
            return tools_reply(call("read_page", path="pages/p1.md"))
        name, price = _spans(request)
        if len(texts) == 1:
            wrong = {
                # The span ID and quote beside the value, not inside "cite".
                "seller": {"value": "SYN-Vendor", "span": name, "quote": NAME},
                # A quote too short to locate the price.
                "price": {"value": "279 EUR", "cite": {"span": price, "quote": "279 EUR"}},
                "warranty": UNKNOWN,
            }
            return tools_reply(call("save_artifact", kind="offer", content=wrong))
        right = {
            "seller": {"value": "SYN-Vendor", "cite": {"span": name, "quote": NAME}},
            "price": {"value": "279 EUR", "cite": {"span": price, "quote": "279 EUR per unit"}},
            "warranty": UNKNOWN,
        }
        return tools_reply(call("save_artifact", 2, kind="offer", content=right), FINISH)

    yield Recorder(rule=answer)


def test_the_model_is_told_how_to_cite_before_its_first_save(
    tmp_path: Path, model: Recorder
) -> None:
    assert _run(tmp_path, model).outcome == "complete"
    (tool,) = [t for t in model.requests[0].tools if t.name == "save_artifact"]
    text = tool.description
    assert "How to cite" in text
    assert '{"cite": {"span": "EX-0042", "quote": "..."}}' in text
    assert "inside `cite`, never beside the value" in text
    assert "citation_spans" in text and "at least 12 characters" in text
    assert '{"unknown": {"question": ' in text and "not_checked" in text
    assert "Example (invented)" in text
    # The read result says the same beside the span IDs it lists.
    read = _tool_texts(model.requests[1])[0].partition("\n")[2]
    assert json.loads(read)["cite_as"].startswith('{"cite": {"span": "<one of citation_spans>"')


def test_a_wrong_cite_is_refused_with_every_rule_and_one_retry_recovers(
    tmp_path: Path, model: Recorder
) -> None:
    result = _run(tmp_path, model)
    assert result.outcome == "complete"
    assert len(model.requests) == 3  # read, the wrong save, the fixed save
    refused = json.loads(_tool_texts(model.requests[2])[1].partition("\n")[0])
    assert refused["status"] == "error" and refused["code"] == "ungrounded_value"
    assert refused["paths"] == ["/content/seller", "/content/price/cite"]
    message = refused["message"]
    assert "2 grounding errors, fix them all" in message
    # The misplaced cite: the rule, the shape to write, and the named span's text.
    assert 'quote, span go inside "cite", not beside the value' in message
    assert '{"cite": {"span": "<ID>", "quote": "<exact words>"}}' in message
    assert f"reads: {NAME!r}" in message
    # The short quote: its length, the rule, and the whole span to copy from.
    assert "quote_too_short: the quote '279 EUR' has 7 characters" in message
    assert "at least 12 characters exactly from" in message and repr(PRICE) in message


def test_a_recorded_turn_factor_scales_the_task_turn_limit(tmp_path: Path) -> None:
    def reads_then_saves() -> Recorder:
        reads = [tools_reply(call("read_file", n, path="a.py")) for n in range(1, 5)]
        return Recorder([*reads, tools_reply(call("save_artifact", kind="note",
                                                   content={"done": True}), FINISH)])  # fmt: skip

    own = Bench(tmp_path / "own.sqlite", reads_then_saves(), turn_limit=2)
    own.start()
    assert own.drive().outcome == "blocked"
    (hit,) = [d for d in own.decisions() if d.payload.get("limit") == "turn_limit"]
    assert dict(hit.payload["counts"]) == {"turns": 2, "allowed": 2}
    own.close()

    live = Bench(tmp_path / "live.sqlite", reads_then_saves(), turn_limit=2)
    live.start(turn_factor=3)
    assert live.drive().outcome == "complete"  # five turns, under 2 x 3
    assert [e.payload["kind"] for e in live.events("artifact.saved")] == [f"{CODER}.note"]
    live.close()
