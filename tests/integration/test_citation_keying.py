"""Invalidation keyed on citations (ADR-P05 amendment M2), through the real agent loop.

A small agent reads a folder of synthetic pages. ``note`` reads a page and saves a claim
citing its first paragraph; ``survey`` runs two recorded searches and saves an absence
claim scoped to one of them; ``facts`` asks an extraction alias for a page's headline and
saves it citing the passage that states it. Each test changes one thing and checks what
reopens:

1. a changed paragraph no output cites reopens nothing: the reads are ``weak``, each
   reader is noticed (``evidence.noticed``) with a low-priority recheck item, and no
   rebuild run opens; under read keying the same edit reopens every reader;
2. a changed cited span reopens the version that cites it, and its rebuild cites the new
   text;
3. a new page matching the absence claim's scoped search reopens the claim; one matching
   only the unscoped search is noticed;
4. a changed extraction schema reopens the value extracted under it, though no page byte
   moved;
5. a ``notify`` agent is noticed strongly for a changed cited span and never reopened.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, ClassVar

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.sources.folder import FolderSource
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactKind, ArtifactRef, InputRef
from aksha.runtime.artifacts.versions import current
from aksha.runtime.definitions.types import Agent
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.ids import LogIdSource
from aksha.runtime.execution.types import StepId
from aksha.runtime.grounding.types import SpanDraft
from aksha.runtime.invalidation.commands import apply_source_change
from aksha.runtime.invalidation.definitions import DefinitionSource
from aksha.runtime.invalidation.keying import notices, recheck_items
from aksha.runtime.invalidation.view import InvalidationView, invalidation_view
from aksha.runtime.loop.drive import drive, drive_rebuilds
from aksha.runtime.loop.types import LoopSetup
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.rules import RuleExtractionModel
from aksha.runtime.models.types import FieldSpec, GenerationRequest, GenerationResult, ModelAlias
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import Task
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import Ok, ToolContext, ToolDecl, ToolOutcome, ToolSpec, ToolUse
from tests.support.events import START
from tests.support.loop import Recorder, call, tools_reply
from tests.support.tasks import has

OWNER = "o1"
AGENT = "keyed"
SOURCE = "docs"
RUN = "R-0001"

P1 = (
    "Headline: SYN-Sensor ships in March.\n\n"
    "The SYN-Sensor counts people without a camera.\n\n"
    "Its battery lasts two years."
)
P2 = "A second synthetic page about mounting brackets."

FACTS = ModelAlias(
    "facts", "extraction", fields=(FieldSpec("headline", description="the page's headline"),)
)
FACTS_V2 = ModelAlias(
    "facts",
    "extraction",
    fields=(FieldSpec("headline", description="the page's headline, without its date"),),
)


class Headlines(RuleExtractionModel):
    """The synthetic extraction model: the text after ``Headline:``."""

    patterns: ClassVar[Mapping[str, str]] = {"headline": r"Headline: ([^\n]+)"}


PATH = {
    "type": "object",
    "properties": {"path": {"type": "string", "minLength": 1}},
    "required": ["path"],
    "additionalProperties": False,
}
TERM = {
    "type": "object",
    "properties": {"term": {"type": "string", "minLength": 1}},
    "required": ["term"],
    "additionalProperties": False,
}
OBJECT: Mapping[str, Any] = {"type": "object"}
KINDS = {f"{AGENT}.{k}": ArtifactKind(k, OBJECT) for k in ("note", "survey", "facts")}


def _paragraphs(text: str) -> list[tuple[int, int]]:
    out, at = [], 0
    for part in text.split("\n\n"):
        out.append((at, at + len(part)))
        at += len(part) + 2
    return out


def query_id(term: str) -> str:
    return f"{AGENT}.search?" + json.dumps({"term": term}, sort_keys=True)


def hits_hash(hits: list[str]) -> str:
    return "q-" + hashlib.sha256("\n".join(sorted(hits)).encode()).hexdigest()[:16]


class Pages:
    """The synthetic folder and the agent's tool bodies over it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "pages").mkdir(parents=True)
        self.put("pages/p1.md", P1)
        self.put("pages/p2.md", P2)
        self.source = FolderSource(SOURCE, root)

    def put(self, rel: str, text: str) -> None:
        (self.root / rel).write_text(text, encoding="utf-8")

    def text(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def hits(self, term: str) -> list[str]:
        return sorted(
            r for r in self.source.records(SOURCE, "pages") if term in self.text(r).lower()
        )

    def run_query(self, owner: str, query: str, /) -> str:
        term = json.loads(query.split("?", 1)[1])["term"]
        return hits_hash(self.hits(term))

    def read_page(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        assert ctx.reader is not None
        found = ctx.reader.read(SOURCE, str(args["path"]))
        assert found is not None
        spans = tuple(
            SpanDraft(SOURCE, found.record_id, found.version, a, b)
            for a, b in _paragraphs(found.text)
        )
        data = {"path": found.record_id, "paragraphs": [found.text[a:b] for a, b in
                                                         _paragraphs(found.text)]}  # fmt: skip
        read = InputRef("record", found.record_id, found.version)
        return ToolOutcome(Ok(data, spans=spans, reads=(read,)))

    def search(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        term = str(args["term"]).lower()
        hits = self.hits(term)
        read = InputRef("query", query_id(term), hits_hash(hits))
        return ToolOutcome(Ok({"query": read.id, "hits": hits}, reads=(read,)))

    def extract(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        assert ctx.reader is not None and ctx.models is not None
        found = ctx.reader.read(SOURCE, str(args["path"]))
        assert found is not None
        drafts = [
            SpanDraft(SOURCE, found.record_id, found.version, a, b)
            for a, b in _paragraphs(found.text)
        ]
        (judged,) = ctx.models.extract("facts", found, drafts)
        assert judged.accepted and judged.spans
        n = int(judged.spans[0].span_id[1:])
        data = {"headline": judged.value, "passage": n,
                "paragraphs": [found.text[d.start : d.end] for d in drafts]}  # fmt: skip
        read = InputRef("record", found.record_id, found.version)
        return ToolOutcome(Ok(data, spans=tuple(drafts), reads=(read,)))


def _agent(pages: Pages, keys: str, mode: str) -> Agent:
    def spec(name: str, schema: Mapping[str, Any], **kw: Any) -> ToolSpec:
        return ToolSpec(name, AGENT, "read", f"{name} over the pages.", schema,
                        collections=frozenset({"pages"}), **kw)  # fmt: skip

    tools = (
        ToolDecl(spec("read_page", PATH, mints_citation_spans=True), pages.read_page),
        ToolDecl(spec("search", TERM, recorded_query=True), pages.search),
        ToolDecl(
            spec("extract", PATH, mints_citation_spans=True, model_aliases=frozenset({"facts"})),
            pages.extract,
        ),
    )
    save = "save_artifact"
    return Agent(
        name=AGENT,
        purpose="Keep notes on synthetic pages.",
        instructions="prompts/instructions.md",
        reads=("record:pages",),
        artifacts=tuple(KINDS.values()),
        tools=(*tools, ToolUse(save)),
        tasks=(
            Task(
                "note", extracts=True, tools=("read_page", save), writes=("note",), done=has("note")
            ),
            Task("survey", tools=("search", save), writes=("survey",), done=has("survey")),
            Task(
                "facts",
                extracts=True,
                tools=("extract", save),
                writes=("facts",),
                done=has("facts"),
            ),
        ),
        model_aliases=(FACTS,),
        invalidation=mode,  # type: ignore[arg-type]
        invalidation_keys=keys,  # type: ignore[arg-type]
    )


def _results(request: GenerationRequest) -> list[tuple[dict[str, Any], list[str]]]:
    """Each tool result the request shows: its data and the span IDs it minted."""
    out = []
    for m in request.messages:
        if m.role != "tool" or not m.text:
            continue
        head, _, tail = m.text.partition("\n")
        shown = json.loads(head)
        cites = json.loads(tail)["citation_spans"] if tail.startswith('{"citation') else []
        if shown.get("status") == "ok":
            out.append((dict(shown["data"]), list(cites)))
    return out


def rule(request: GenerationRequest) -> GenerationResult:
    """The model: each Task calls its tools on the first turn, then saves and finishes."""
    instance = str(StepId.parse(request.step_id).instance)
    results = _results(request)
    finish = call("finish", summary="done")
    if instance == "note":
        if not results:
            return tools_reply(call("read_page", path="pages/p1.md"))
        data, cites = results[-1]
        first = data["paragraphs"][1]  # the sentence about the camera
        content = {"claim": {"value": first, "cite": {"span": cites[1], "quote": first}}}
        return tools_reply(call("save_artifact", kind="note", content=content), finish)
    if instance == "survey":
        if not results:
            return tools_reply(call("search", 1, term="lidar"), call("search", 2, term="battery"))
        scoped = next(d["query"] for d, _ in results if "lidar" in d["query"])
        survey: dict[str, Any] = {
            "no_lidar": {
                "absence": {"statement": "no page mentions a lidar", "scope": {"query": scoped}}
            },
            "battery_pages": len(next(d["hits"] for d, _ in results if "battery" in d["query"])),
        }
        return tools_reply(call("save_artifact", kind="survey", content=survey), finish)
    assert instance == "facts"
    if not results:
        return tools_reply(call("extract", path="pages/p1.md"))
    data, cites = results[-1]
    quote = data["paragraphs"][data["passage"]]
    content = {"headline": {"value": data["headline"],
                            "cite": {"span": cites[data["passage"]], "quote": quote}}}  # fmt: skip
    return tools_reply(call("save_artifact", kind="facts", content=content), finish)


class Bench:
    """One owner's store, the pages, and the agent's loop setup."""

    def __init__(self, path: Path, keys: str = "citations", mode: str = "rebuild") -> None:
        self.pages = Pages(path.parent / "pages-root")
        self.clock = FakeClock(START)
        self.store = open_store(path, self.clock, writer=True)
        self.model = Recorder(rule=rule)
        agent = _agent(self.pages, keys, mode)
        listed = {t.spec.name: t for t in agent.tools if isinstance(t, ToolDecl)}
        self.setup = LoopSetup(
            store=self.store, owner=OWNER, agent=agent, tree=compile_tree(AGENT, agent.tasks),
            registry=ToolRegistry(agent, listed, ()),
            table=bind_aliases((FACTS,), "fake", {"generate": self.model, "facts": Headlines()}),
            instructions="Keep notes.", request="Note the pages.", reader=self.pages.source,
            artifacts=ArtifactStore(KINDS), kinds=KINDS,
        )  # fmt: skip
        self.executor = StepExecutor(self.store, OWNER, self.clock)
        payload = {"agent": AGENT, "cause": "request", "invalidation": mode,
                   "invalidation_keys": keys}  # fmt: skip
        self.store.append(OWNER, "U1", (NewEvent("run.started", payload, run=RUN),))
        result = drive(self.setup, self.executor, RUN)
        assert (result.state, result.outcome) == ("ended", "complete"), result

    def close(self) -> None:
        self.store.close()

    def view(self) -> LogView:
        return load_view(self.store, OWNER)

    def iv(self) -> InvalidationView:
        return invalidation_view(self.view())

    def ref(self, kind: str) -> str:
        found = current(self.view(), f"{AGENT}.{kind}", None)
        assert found is not None
        return str(found.ref)

    def change(self, name: str, *sources: Any) -> None:
        held = sources or (self.pages.source,)
        receipt = apply_source_change(self.store, OWNER, held, name,
                                      run_query=self.pages.run_query)  # fmt: skip
        assert receipt.accepted

    def rebuild(self) -> list[str]:
        """Open and drive the rebuild runs; the runs opened."""
        results = drive_rebuilds(self.setup, self.executor, LogIdSource(self.store))
        assert all(r.state == "ended" for r in results), results
        return [r.run for r in results]

    def events(self, type_: str) -> list[Event]:
        return [e for e in self.view().events if e.type == type_]


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    b = Bench(tmp_path / "a.sqlite")
    yield b
    b.close()


def _state(iv: InvalidationView, ref: str, record: str) -> str:
    return next(r.state for r in iv.state(ref).reads if r.input.id == record)


def test_every_output_names_what_it_rests_on(bench: Bench) -> None:
    bound = {(e.payload["carrier"], e.payload["form"]) for e in bench.events("citation.bound")}
    assert (f"{AGENT}.note@v1", "span") in bound
    assert (f"{AGENT}.facts@v1", "span") in bound
    reads = {str(r) for e in bench.events("read.recorded") for r in [e.payload["input"]]}
    # The extraction recorded the schema it ran under, beside the page it read.
    assert any(f"definitions/{AGENT}/facts" in r for r in reads)


def test_a_changed_uncited_paragraph_does_not_reopen(bench: Bench) -> None:
    bench.pages.put("pages/p1.md", P1.replace("two years", "three years"))  # nobody cites it
    bench.change("battery")
    iv = bench.iv()
    for kind in ("note", "facts"):
        ref = bench.ref(kind)
        assert iv.state(ref).freshness == "current", kind
        assert _state(iv, ref, "pages/p1.md") == "weak"
    assert set(iv.summary().noticed) == {f"{AGENT}.note@v1", f"{AGENT}.facts@v1"}
    (rechecked,) = bench.events("excerpts.rechecked")
    assert sorted(rechecked.payload["spans"].values()) == ["changed", "kept", "kept"]
    noticed = notices(bench.view())
    assert {(n.version, n.strength) for n in noticed} == {
        (f"{AGENT}.note@v1", "weak"),
        (f"{AGENT}.facts@v1", "weak"),
    }
    assert all("new evidence may exist" in e.payload["notice"]
               for e in bench.events("evidence.noticed"))  # fmt: skip
    assert len(recheck_items(bench.view())) == 2
    assert bench.rebuild() == []  # nothing reopened, so no rebuild run opens


def test_read_keying_reopens_the_same_uncited_edit(tmp_path: Path) -> None:
    """The control: today's behaviour, every read a dependency."""
    b = Bench(tmp_path / "r.sqlite", keys="reads")
    try:
        b.pages.put("pages/p1.md", P1.replace("two years", "three years"))
        b.change("battery")
        iv = b.iv()
        assert iv.state(b.ref("note")).freshness == "invalidated"
        assert iv.state(b.ref("facts")).freshness == "invalidated"
        assert b.events("evidence.noticed") == []
    finally:
        b.close()


def test_a_changed_cited_span_reopens(bench: Bench) -> None:
    bench.pages.put("pages/p1.md", P1.replace("without a camera", "with a thermal camera"))
    bench.change("camera")
    iv = bench.iv()
    note = iv.state(f"{AGENT}.note@v1")
    assert note.freshness == "invalidated"
    assert [r.input.id for r in note.changed_reads()] == ["pages/p1.md"]
    assert iv.state(f"{AGENT}.facts@v1").freshness == "current"  # it cites the headline
    assert bench.rebuild()
    rebuilt = current(bench.view(), f"{AGENT}.note", None)
    assert rebuilt is not None and rebuilt.ref == ArtifactRef(f"{AGENT}.note", None, 2)
    assert "thermal camera" in rebuilt.content["claim"]["value"]
    assert bench.iv().state(str(rebuilt.ref)).freshness == "current"


def test_a_new_source_matching_an_absence_scope_reopens(bench: Bench) -> None:
    bench.pages.put("pages/p3.md", "A synthetic page: the SYN-Lidar counts desks.")
    bench.change("lidar")
    iv = bench.iv()
    survey = iv.state(f"{AGENT}.survey@v1")
    assert survey.freshness == "invalidated"
    assert [r.input.id for r in survey.changed_reads()] == [query_id("lidar")]


def test_a_new_source_matching_only_an_unscoped_search_is_noticed(bench: Bench) -> None:
    bench.pages.put("pages/p3.md", "A synthetic page: a battery lasts a year.")
    bench.change("battery-page")
    iv = bench.iv()
    survey = iv.state(f"{AGENT}.survey@v1")
    assert survey.freshness == "current"
    assert [(r.input.id, r.state) for r in survey.weak_reads()] == [(query_id("battery"), "weak")]
    assert f"{AGENT}.survey@v1" in {n.version for n in notices(bench.view())}


def test_a_changed_definition_reopens_the_values_extracted_under_it(bench: Bench) -> None:
    before = bench.pages.text("pages/p1.md")
    bench.change("schema-v2", DefinitionSource({AGENT: (FACTS_V2,)}))
    assert bench.pages.text("pages/p1.md") == before  # no source byte moved
    iv = bench.iv()
    facts = iv.state(f"{AGENT}.facts@v1")
    assert facts.freshness == "invalidated"
    assert [r.input.id for r in facts.changed_reads()] == [f"definitions/{AGENT}/facts"]
    assert iv.state(f"{AGENT}.note@v1").freshness == "current"  # no model read it


def test_notify_mode_is_noticed_strongly_and_never_reopened(tmp_path: Path) -> None:
    b = Bench(tmp_path / "n.sqlite", mode="notify")
    try:
        b.pages.put("pages/p1.md", P1.replace("without a camera", "with a thermal camera"))
        b.change("camera")
        assert b.iv().state(b.ref("note")).freshness == "current"  # notify never rebuilds
        by = {n.version: n for n in notices(b.view())}
        assert by[f"{AGENT}.note@v1"].strength == "strong"
        assert by[f"{AGENT}.facts@v1"].strength == "weak"
        assert recheck_items(b.view())[0].version == f"{AGENT}.facts@v1"
    finally:
        b.close()
