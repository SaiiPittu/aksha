"""The harness's input tools over a small in-memory source: windows, units, extraction, search.

Each guarantee has a test that fails when the code enforcing it goes: a window cut by its
caps says where to continue, an offset past the end says how many lines there are, every
unit is minted as one span over the returned text, a listing and a search mint nothing and
record a query over their hit set, ``where`` reads a record part, and ``extract`` returns
typed values each cited to a unit of exactly its range.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from aksha.runtime.grounding.reading import AttemptReader
from aksha.runtime.grounding.types import ReturnedText
from aksha.runtime.inputs.tools import InputReading, input_tools
from aksha.runtime.models.documents import DocumentKind
from aksha.runtime.models.fixed import distribution_ppm, ppm
from aksha.runtime.models.offline import OfflineReaders
from aksha.runtime.models.rules import RuleClassificationModel, RuleExtractionModel
from aksha.runtime.models.types import (
    ClassAnswer,
    ClassificationRequest,
    ClassificationResult,
    ExtractionRequest,
    ExtractionResult,
    FieldAnswer,
    FieldSpec,
    ModelAlias,
    SpanRef,
)
from aksha.runtime.tools.types import Err, Ok, ToolContext

NOTE = "# Plan\n\nWe sell the probe.\n\n## Prices\n\n- The probe costs EUR 49 per year.\n"
TABLE = "id,owner,what\nr1,A,first row\nr2,B,second row\nr3,A,third row\n"
CONVERTED = (
    '[document D-1 v1] "a.pdf" (name as uploaded)\n\n'
    "[p.1]\nIntro text here.\n[p.2]\nResults text here.\n"
)


class Source:
    """An in-memory versioned source: record ID to text, versioned by the text itself."""

    def __init__(self, texts: Mapping[str, str]) -> None:
        self.texts = dict(texts)

    def read(self, source: str, record_id: str) -> ReturnedText | None:
        text = self.texts.get(record_id)
        return None if text is None else ReturnedText(source, record_id, f"v{len(text)}", text)

    def records(self, source: str, collection: str) -> tuple[str, ...]:
        return tuple(sorted(self.texts))

    def serves(self, source: str) -> bool:
        return True


class Kind(RuleClassificationModel):
    """``note`` for Markdown, ``other`` for anything else."""

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        q = request.questions[0]
        pick = "note" if "format: markdown" in request.state else "other"
        rest = (Decimal(1) - Decimal("0.95")) / (len(q.options) - 1)
        probs = {o: Decimal("0.95") if o == pick else rest for o in q.options}
        return ClassificationResult(answers=((q.name, ClassAnswer(pick, distribution_ppm(probs))),))


class Prices(RuleExtractionModel):
    """``prices``: each ``EUR <n>`` stated in a passage."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        spans = []
        for s in request.state:
            at = s.text.find("EUR ")
            if at >= 0:
                spans.append(SpanRef(s.span_id, at, at + len("EUR 49")))
        value = tuple("EUR 49" for _ in spans)
        answer = FieldAnswer(value, tuple(spans), ppm(Decimal("0.01")), ppm(Decimal("0.95")))
        return ExtractionResult(answers=(("prices", answer),))


KINDS = (DocumentKind("note", "a note", "prices"),)
ALIASES = (
    ModelAlias("kind", "classification"),
    ModelAlias("prices", "extraction", fields=(FieldSpec("prices", "list"),)),
)
READING = InputReading("inputs", "input", decide="kind", kinds=KINDS, page_lines=3)


def _tools(reading: InputReading = READING) -> dict[str, Any]:
    return {t.spec.name: t for t in input_tools("tester", reading)}


def _ctx(reader: Any, run_date: str | None = None, environment: str = "") -> ToolContext:
    models = OfflineReaders({"kind": Kind(), "prices": Prices()}, ALIASES)
    return ToolContext(
        "o",
        "tester",
        "t",
        None,
        "s",
        1,
        reader=reader,
        models=models,
        run_date=run_date,
        environment=environment,
    )


def _call(name: str, reader: Any, args: dict[str, Any], **kw: Any) -> Ok | Err:
    body = _tools(kw.pop("reading", READING))[name].body
    return body(_ctx(reader, **kw), args).result  # type: ignore[no-any-return]


@pytest.fixture
def source() -> Source:
    return Source({"input/note.md": NOTE, "input/t.csv": TABLE, "input/a.pdf": CONVERTED})


def test_the_three_tools_are_reads_of_the_declared_collection() -> None:
    tools = _tools()
    assert list(tools) == ["list_inputs", "read_document", "search_inputs"]
    for t in tools.values():
        assert t.spec.effect == "read" and t.spec.collections == frozenset({"input"})
    assert tools["read_document"].spec.mints_citation_spans
    assert tools["list_inputs"].spec.recorded_query and tools["search_inputs"].spec.recorded_query
    assert tools["read_document"].spec.model_aliases == {"kind", "prices"}


def test_a_listing_names_kind_size_version_and_mints_nothing(source: Source) -> None:
    got = _call("list_inputs", source, {}, run_date="2026-11-27")
    assert isinstance(got, Ok) and got.spans == ()
    rows = {r["ref"]: r for r in got.data["inputs"]}
    assert rows["input/note.md"]["kind"] == "note" and rows["input/t.csv"]["kind"] == "other"
    assert rows["input/t.csv"]["columns"] == ["id", "owner", "what"]
    assert rows["input/note.md"]["chars"] == len(NOTE)
    assert got.data["as_of"] == "2026-11-27"
    (read,) = got.reads
    assert read.kind == "query" and read.id == "tester.list_inputs?{}"


def test_the_environment_s_note_on_the_records_comes_with_every_input_result(
    source: Source,
) -> None:
    """A world's note that its records are a synthetic sample reaches the model from the
    environment, with each listing, read and search; with no note, nothing is added."""
    note = "These records are a synthetic sample built for this exercise."
    calls: list[tuple[str, dict[str, Any]]] = [
        ("list_inputs", {}),
        ("read_document", {"ref": "input/note.md"}),
        ("search_inputs", {"query": "probe"}),
    ]
    for name, args in calls:
        got = _call(name, source, dict(args), environment=note)
        assert isinstance(got, Ok) and got.data["environment"] == note, name
        bare = _call(name, source, dict(args))
        assert isinstance(bare, Ok) and "environment" not in bare.data, name


def test_a_listing_version_is_its_hit_set(source: Source) -> None:
    def version() -> str:
        got = _call("list_inputs", source, {"kinds": ["note"]})
        assert isinstance(got, Ok)
        return str(got.reads[0].version)

    first = version()
    source.texts["input/u.csv"] = "a,b\n1,2\n"  # not a note: the listing of notes did not move
    assert version() == first
    source.texts["input/other.md"] = "# Another\n"
    assert version() != first


def test_a_window_is_numbered_lines_and_says_where_to_continue(source: Source) -> None:
    got = _call("read_document", source, {"ref": "input/note.md"})
    assert isinstance(got, Ok)
    assert [n for n, _ in got.data["lines"]] == [1, 2, 3]
    assert got.data["next_offset"] == 4 and "offset=4" in got.data["truncated"]
    rest = _call("read_document", source, {"ref": "input/note.md", "offset": 4, "limit": 50})
    assert isinstance(rest, Ok) and "next_offset" not in rest.data
    whole = "\n".join(t for _, t in got.data["lines"] + rest.data["lines"])
    assert whole == NOTE


def test_an_offset_past_the_end_says_how_many_lines(source: Source) -> None:
    got = _call("read_document", source, {"ref": "input/note.md", "offset": 99})
    assert isinstance(got, Err) and got.code == "offset_past_end"
    assert f"lines 1-{NOTE.count(chr(10)) + 1}" in got.message


def test_the_character_cap_cuts_a_window(source: Source) -> None:
    tight = InputReading("inputs", "input", page_lines=100, page_chars=12, line_chars=5)
    got = _call("read_document", source, {"ref": "input/note.md"}, reading=tight)
    assert isinstance(got, Ok)
    assert "cut at 5" in got.data["truncated"] and got.data["next_offset"] is not None
    assert all(len(t) <= 5 for _, t in got.data["lines"])


def test_every_unit_is_one_span_over_the_returned_text(source: Source) -> None:
    reader = AttemptReader(source)
    got = _call("read_document", reader, {"ref": "input/note.md", "limit": 50})
    assert isinstance(got, Ok)
    (returned,) = reader.returned
    assert len(got.spans) == len(got.data["units"]) > 0
    for d, u in zip(got.spans, got.data["units"], strict=True):
        assert (d.record_id, d.version, d.start, d.end) == (
            returned.record_id,
            returned.version,
            u[4],
            u[5],
        )
    texts = [returned.text[u[4] : u[5]] for u in got.data["units"]]
    assert "Prices" in texts and "## Prices" in texts  # the block, and its whole line
    assert [str(r) for r in got.reads] == [f"record:input/note.md@{returned.version}"]


def test_anchors_come_from_pages_and_headings(source: Source) -> None:
    got = _call("read_document", source, {"ref": "input/a.pdf", "limit": 50})
    assert isinstance(got, Ok)
    assert [a[0] for a in got.data["anchors"]] == ["p.1", "p.2"]
    results = [u for u in got.data["units"] if CONVERTED[u[4] : u[5]] == "Results text here."]
    assert results and results[0][6] == "p.2"


def test_extract_returns_typed_values_each_cited_to_its_own_unit(source: Source) -> None:
    got = _call("read_document", source, {"ref": "input/note.md", "limit": 50, "extract": True})
    assert isinstance(got, Ok)
    assert (got.data["kind"], got.data["decided"]) == ("note", True)
    prices = got.data["fields"]["prices"]
    assert prices["value"] == ["EUR 49"]
    (n,) = prices["units"]
    unit = got.data["units"][n]
    assert NOTE[unit[4] : unit[5]] == "EUR 49" and unit[1] == "field:prices"
    assert (got.spans[n].start, got.spans[n].end) == (unit[4], unit[5])


def test_extract_needs_declared_kinds(source: Source) -> None:
    bare = InputReading("inputs", "input")
    assert "extract" not in _tools(bare)["read_document"].spec.schema["properties"]
    got = _call("read_document", source, {"ref": "input/note.md", "extract": True}, reading=bare)
    assert isinstance(got, Err) and got.code == "no_extraction"


def test_where_reads_a_record_part_at_its_version(source: Source) -> None:
    got = _call(
        "read_document",
        source,
        {"ref": "input/t.csv", "where": {"column": "owner", "values": ["A"]}},
    )
    assert isinstance(got, Ok)
    assert [t for _, t in got.data["lines"]] == [
        "id,owner,what",
        "r1,A,first row",
        "r3,A,third row",
    ]
    (read,) = got.reads
    assert read.id == "input/t.csv#where:owner=A" and read.version.startswith("part:")
    source.texts["input/t.csv"] = TABLE.replace("second row", "changed")  # another owner's row
    again = _call(
        "read_document",
        source,
        {"ref": "input/t.csv", "where": {"column": "owner", "values": ["A"]}},
    )
    assert isinstance(again, Ok) and again.reads[0].version == read.version
    bad = _call(
        "read_document",
        source,
        {"ref": "input/t.csv", "where": {"column": "nope", "values": ["A"]}},
    )
    assert isinstance(bad, Err) and bad.code == "no_column" and "owner" in bad.message


@pytest.mark.parametrize(
    ("ref", "code"),
    [
        ("https://x.example/a", "not_an_input"),
        ("input/../x", "not_an_input"),
        ("input/missing.md", "not_found"),
    ],
)
def test_a_ref_that_names_no_input_is_refused(source: Source, ref: str, code: str) -> None:
    got = _call("read_document", source, {"ref": ref})
    assert isinstance(got, Err) and got.code == code


def test_a_short_ref_names_its_input(source: Source) -> None:
    got = _call("read_document", source, {"ref": "note.md"})
    assert isinstance(got, Ok) and got.data["ref"] == "input/note.md"


def test_search_ranks_passages_mints_nothing_and_records_its_hit_set(source: Source) -> None:
    got = _call("search_inputs", source, {"query": "row third"})
    assert isinstance(got, Ok) and got.spans == ()
    hits = got.data["hits"]
    assert hits[0]["ref"] == "input/t.csv" and "third row" in hits[0]["snippet"]
    assert hits[0]["score"] > hits[-1]["score"]  # two words beat one
    phrase = _call("search_inputs", source, {"terms": ["second row"]})
    assert isinstance(phrase, Ok) and [h["lines"] for h in phrase.data["hits"]] == [[3, 3]]
    (read,) = got.reads
    assert read.kind == "query" and got.data["recorded"] == f"query:{read.id}@{read.version}"
    none = _call("search_inputs", source, {"query": "absent words", "match": "all"})
    assert isinstance(none, Ok) and none.data["hits"] == []


def test_search_by_kind_searches_only_the_inputs_of_those_kinds() -> None:
    """A search over one kind hits and records only those inputs: another kind moves nothing."""
    source = Source({"input/note.md": NOTE, "input/plain.txt": "The probe is cheap.\n"})
    by_kind = replace(READING, search_kinds=True)
    every = _call("search_inputs", source, {"query": "probe"})
    notes = _call("search_inputs", source, {"query": "probe", "kinds": ["note"]}, reading=by_kind)
    assert isinstance(every, Ok) and isinstance(notes, Ok)
    assert every.data["inputs_hit"] == ["input/note.md", "input/plain.txt"]
    assert notes.data["inputs_hit"] == ["input/note.md"] and notes.data["inputs_searched"] == 1
    source.texts["input/other.txt"] = "Another probe page.\n"  # a new input of another kind
    again = _call("search_inputs", source, {"query": "probe", "kinds": ["note"]}, reading=by_kind)
    assert isinstance(again, Ok) and again.reads == notes.reads
    assert "kinds" in _tools(by_kind)["search_inputs"].spec.schema["properties"]
    # off by default: an agent that does not search by kind keeps its recorded tool list
    assert "kinds" not in _tools()["search_inputs"].spec.schema["properties"]
    refused = _call("search_inputs", source, {"query": "probe", "kinds": ["note"]})
    assert isinstance(refused, Err) and refused.code == "no_kinds"


def test_a_window_is_cut_to_fit_the_agent_result_cap() -> None:
    long = "".join(f"line {n} " + "x" * 60 + "\n" for n in range(200))
    source = Source({"input/long.txt": long})
    capped = InputReading(
        "inputs", "input", page_lines=2_000, page_chars=50_000, result_chars=4_000
    )
    got = _call("read_document", source, {"ref": "input/long.txt"}, reading=capped)
    assert isinstance(got, Ok)
    assert len(json.dumps(got.data)) + 24 * len(got.spans) <= 4_000
    assert got.data["next_offset"] > 1 and "continue" in got.data["truncated"]


def test_a_where_read_is_held_to_the_window_and_result_caps() -> None:
    rows = "".join(f"r{n},A,{'y' * 80}\n" for n in range(400))
    source = Source({"input/big.csv": "id,owner,what\n" + rows})
    capped = InputReading(
        "inputs", "input", page_lines=2_000, page_chars=50_000, result_chars=4_000
    )
    got = _call(
        "read_document",
        source,
        {"ref": "input/big.csv", "where": {"column": "owner", "values": ["A"]}},
        reading=capped,
    )
    assert isinstance(got, Ok)
    assert len(json.dumps(got.data)) + 24 * len(got.spans) <= 4_000
    assert "narrow" in got.data["truncated"] and got.data["rows"] < 400
    last = got.data["lines"][-1][0]
    assert all(row[3] <= last for row in got.data["units"])  # no unit past the window
    (read,) = got.reads
    assert read.id == "input/big.csv#where:owner=A"  # the part is still the read
