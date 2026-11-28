"""A run asks each fast-class request once (``models.calls.FastAnswers``), through the real loop.

A small agent lists its inputs with the harness's ``list_inputs`` and reads one with
``read_document``. Its decide alias is a counting double, so each test can say how many
times the adapter was really asked, and for what:

1. one listing decides a multi-section note once for the whole record and once per section,
   never once per passage; listing again and reading the note asks nothing more;
2. a listing whose attempt fails on a transient error part-way is retried, and the retry asks
   only what the failed attempt had not been answered;
3. a remembered answer is recorded on its step with no usage, so the run's spend counts each
   answer once, while the step's summary still counts every decision the body used; an
   answer a failed attempt paid for is booked by the attempt that completes, so the run's
   completed steps sum to what every answer cost.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.sources.folder import FolderSource
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactKind
from aksha.runtime.definitions.types import Agent
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.types import StepId
from aksha.runtime.grounding.segment import segment
from aksha.runtime.inputs.tools import InputReading, input_tools
from aksha.runtime.loop.drive import drive
from aksha.runtime.loop.types import LoopSetup
from aksha.runtime.models.documents import DocumentKind, record_sections
from aksha.runtime.models.errors import TransientModelError
from aksha.runtime.models.fixed import distribution_ppm
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.rules import RuleClassificationModel
from aksha.runtime.models.types import (
    ClassAnswer,
    ClassificationRequest,
    ClassificationResult,
    GenerationRequest,
    GenerationResult,
    ModelAlias,
    Usage,
)
from aksha.runtime.projections.view import load_view
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import Task
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import ToolDecl, ToolUse
from tests.support.events import START
from tests.support.loop import Recorder, call, tools_reply
from tests.support.tasks import has

OWNER = "o1"
AGENT = "reader"
RUN = "R-0001"
DECIDE = ModelAlias("doc_kind", "classification", on_exhausted="unknown")
KINDS = (DocumentKind("note", "the person's own note"), DocumentKind("table", "a table"))
NOTE = {f"{AGENT}.note": ArtifactKind("note", {"type": "object"})}

PLAN = "\n\n".join(
    f"## SYN section {n}\n\n" + "\n\n".join(f"SYN paragraph {n}.{p} of the plan." for p in range(3))
    for n in range(3)
)
"""A synthetic note of three sections, each a heading and three paragraphs."""
TABLE = "name,price\nSYN-A,9\nSYN-B,12\n"


class Counted(RuleClassificationModel):
    """The decide double: answers ``note`` with certainty, keeps each request's hash.

    ``fail_at`` raises one transient error on that call (1-based), as a stalled provider
    call would; ``usage`` is what each answer reports it spent.
    """

    def __init__(self, fail_at: int | None = None, usage: Usage | None = None) -> None:
        self.asked: list[str] = []
        self.fail_at = fail_at
        self.usage = usage or Usage()

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        self.asked.append(request.request_hash)
        if len(self.asked) == self.fail_at:
            raise TransientModelError(message="SYN provider timeout")
        q = request.questions[0]
        weights = {o: (1 if o == "note" else 0) for o in q.options}
        answer = ClassAnswer("note", distribution_ppm(weights))
        return ClassificationResult(((q.name, answer),), self.usage)


def rule(request: GenerationRequest) -> GenerationResult:
    """List, read the note, list again; then save and finish."""
    turn = StepId.parse(request.step_id).turn
    if turn == 1:
        return tools_reply(
            call("list_inputs", 1),
            call("read_document", 1, ref="notes/plan.md"),
            call("list_inputs", 2),
        )
    return tools_reply(
        call("save_artifact", kind="note", content={"read": True}), call("finish", summary="ok")
    )


def _agent() -> Agent:
    reading = InputReading("inputs", "notes", decide="doc_kind", kinds=KINDS)
    tools = input_tools(AGENT, reading)
    return Agent(
        name=AGENT,
        purpose="Read synthetic notes.",
        instructions="prompts/instructions.md",
        reads=("record:notes",),
        artifacts=tuple(NOTE.values()),
        tools=(*tools, ToolUse("save_artifact")),
        tasks=(
            Task(
                "read",
                tools=("list_inputs", "read_document", "save_artifact"),
                writes=("note",),
                done=has("note"),
            ),
        ),
        model_aliases=(DECIDE,),
    )


class Bench:
    """One run of the reader over a folder holding the note and a table."""

    def __init__(self, tmp: Path, decide: Counted) -> None:
        root = tmp / "inputs"
        root.mkdir(parents=True)
        (root / "plan.md").write_text(PLAN, encoding="utf-8")
        (root / "prices.csv").write_text(TABLE, encoding="utf-8")
        self.decide = decide
        self.store = open_store(tmp / "aksha.sqlite", FakeClock(START), writer=True)
        agent = _agent()
        listed = {t.spec.name: t for t in agent.tools if isinstance(t, ToolDecl)}
        table = bind_aliases((DECIDE,), "fake", {"generate": Recorder(rule=rule),
                                                 "doc_kind": decide})  # fmt: skip
        self.setup = LoopSetup(
            store=self.store, owner=OWNER, agent=agent, tree=compile_tree(AGENT, agent.tasks),
            registry=ToolRegistry(agent, listed, ()), table=table,
            instructions="Read the notes.", request="Read the notes.",
            reader=FolderSource("inputs", root, "notes"), artifacts=ArtifactStore(NOTE),
            kinds=NOTE,
        )  # fmt: skip
        started = NewEvent("run.started", {"agent": AGENT, "cause": "request"}, run=RUN)
        self.store.append(OWNER, "U1", (started,))
        self.result = drive(self.setup, StepExecutor(self.store, OWNER, FakeClock(START)), RUN)

    def completed(self, tool: str) -> list[Mapping[str, Any]]:
        """The ``step.completed`` payloads of ``tool``'s steps, in log order."""
        view = load_view(self.store, OWNER)
        named = {e.step for e in view.events if e.type == "step.started"
                 and e.payload.get("name") == tool}  # fmt: skip
        return [e.payload for e in view.events if e.type == "step.completed" and e.step in named]


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Any]:
    made: list[Bench] = []

    def make(decide: Counted) -> Bench:
        b = Bench(tmp_path / f"b{len(made)}", decide)
        made.append(b)
        return b

    yield make
    for b in made:
        b.store.close()


def _sections() -> int:
    return len(record_sections(segment(PLAN, "markdown")))


def test_a_multi_section_note_is_decided_per_section_and_each_request_once(bench: Any) -> None:
    b = bench(Counted())
    assert (b.result.state, b.result.outcome) == ("ended", "complete"), b.result
    passages = len(segment(PLAN, "markdown"))
    sections = _sections()
    assert sections == 3 < passages
    # The note once whole and once per section, the table once: never once per passage.
    assert len(set(b.decide.asked)) == 1 + sections + 1
    # Listing again and reading the note asks nothing the run was already answered.
    assert len(b.decide.asked) == len(set(b.decide.asked)), "a request was asked twice"
    listings = b.completed("list_inputs")
    assert len(listings) == 2
    assert listings[0]["value"] == listings[1]["value"], "the second listing reads the same"


def test_a_retried_listing_reuses_the_decisions_its_failed_attempt_had(bench: Any) -> None:
    b = bench(Counted(fail_at=3, usage=Usage(input_tokens=100)))
    assert (b.result.state, b.result.outcome) == ("ended", "complete"), b.result
    view = load_view(b.store, OWNER)
    failed = [e for e in view.events if e.type == "step.failed"]
    assert len(failed) == 1, "the first listing's first attempt fails once"
    distinct = len(set(b.decide.asked))
    # Two answers before the failure, the failed call itself, then only what was left.
    assert len(b.decide.asked) == distinct + 1
    assert b.decide.asked.count(b.decide.asked[2]) == 2, "the failed request is asked again"
    # Every answered call (all but the one that raised) is booked once by a completed step.
    spent = sum(
        c["usage"]["input_tokens"]
        for e in view.events
        if e.type == "step.completed"
        for c in (e.payload.get("value") or {}).get("model_calls", ())
    )
    assert spent == 100 * (len(b.decide.asked) - 1)


def test_a_remembered_answer_spends_nothing_and_still_counts_as_used(bench: Any) -> None:
    b = bench(Counted(usage=Usage(input_tokens=100, output_tokens=1)))
    first, second = b.completed("list_inputs")
    (one,) = first["value"]["model_calls"]
    (two,) = second["value"]["model_calls"]
    assert one["calls"] == two["calls"] == 1 + _sections() + 1
    assert one["usage"]["input_tokens"] == 100 * one["calls"]
    assert two["usage"] == {"input_tokens": 0, "output_tokens": 0, "cost_micro_usd": 0}
    assert one["calls_hash"] == two["calls_hash"]
