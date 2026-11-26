"""The research tools read only through the runtime's reader, and only the drafts they mint
are citable: a read page's passages are, a listing or a search snippet is not. No tool
expects a folder: every input is one record of the ``input`` collection, read by format."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aksha.adapters.sources.folder import FolderSource
from aksha.agents.research import tools
from aksha.runtime.grounding.reading import AttemptReader
from aksha.runtime.tools.types import Err, Ok, ToolContext
from tests.support.research import WORLDS

SOURCES = WORLDS / "stillroom" / "sources"
PAGE = "input/competitors/comp01-kelvra.html"


def _ctx(reader: Any) -> ToolContext:
    return ToolContext("o", "research", "evidence", "k", "R-0001/evidence[k]/t01/c01", 1, reader)


@pytest.fixture
def reader() -> AttemptReader:
    return AttemptReader(FolderSource(tools.SOURCE, SOURCES, tools.COLLECTION))


def test_fetch_page_drafts_one_span_per_passage_over_the_returned_text(
    reader: AttemptReader,
) -> None:
    out = tools.fetch_page(_ctx(reader), {"record": PAGE})
    assert isinstance(out.result, Ok)
    (returned,) = reader.returned
    passages = out.result.data["passages"]
    assert len(out.result.spans) == len(passages)
    for draft, passage in zip(out.result.spans, passages, strict=True):
        assert (draft.source, draft.record_id, draft.version) == returned.locator()
        assert returned.text[draft.start : draft.end] == passage["text"]
    assert [str(r) for r in out.result.reads] == [f"record:{PAGE}@{returned.version}"]
    assert returned.version.startswith("sha256:")


def test_list_inputs_finds_pages_and_review_tables_by_content_not_by_folder(
    reader: AttemptReader,
) -> None:
    out = tools.list_inputs(_ctx(reader), {})
    assert isinstance(out.result, Ok) and out.result.spans == ()
    shapes = {r["record"]: r["shape"] for r in out.result.data["inputs"]}
    assert len(shapes) == sum(1 for p in SOURCES.rglob("*") if p.is_file())
    sellers = sorted(r for r, s in shapes.items() if s == "seller_page")
    assert sellers == sorted(
        f"input/competitors/{p.name}" for p in (SOURCES / "competitors").iterdir()
    )
    assert [r for r, s in shapes.items() if s == "review_table"] == ["input/reviews/reviews.json"]
    assert shapes["input/contributors/records.csv"] == "table"  # text, but no rating: not reviews
    (read,) = out.result.reads
    assert (read.kind, read.id) == ("query", "research.list_inputs?{}")


def test_a_listing_is_a_recorded_query_whose_version_is_its_hit_set(tmp_path: Path) -> None:
    page = (
        '<meta name="published" content="2026-01-01"><title>A</title><body><h1>A</h1>'
        "<table><tr><th>Item</th><th>Price</th></tr>"
        "<tr><td>A</td><td>EUR 5 per unit</td></tr></table></body>"
    )
    (tmp_path / "a.html").write_text(page, encoding="utf-8")
    folder = FolderSource(tools.SOURCE, tmp_path, tools.COLLECTION)
    args = {"shapes": ["seller_page"]}

    def version() -> str:
        out = tools.list_inputs(_ctx(folder), args)
        assert isinstance(out.result, Ok)
        return out.result.reads[0].version

    first = version()
    (tmp_path / "note.md").write_text("# A note\n\nNothing for sale.\n", encoding="utf-8")
    assert version() == first  # a new note is no new seller page: the listing did not move
    (tmp_path / "b.md").write_text("---\nproduct: B\n---\n# B\n", encoding="utf-8")
    assert version() != first


def test_search_mints_nothing_and_records_only_its_hit_set(reader: AttemptReader) -> None:
    out = tools.search(_ctx(reader), {"terms": ["occupancy"], "formats": ["html"]})
    assert isinstance(out.result, Ok)
    assert out.result.spans == ()
    data = out.result.data
    assert data["records_checked"] == 6 and data["hits"]
    assert "not citable" in data["note"]
    (read,) = out.result.reads
    assert read.kind == "query" and data["query"] == f"query:{read.id}@{read.version}"


def test_search_with_no_hit_still_records_its_query(reader: AttemptReader) -> None:
    out = tools.search(_ctx(reader), {"terms": ["spreadsheet"], "formats": ["html"]})
    assert isinstance(out.result, Ok)
    assert out.result.data["hits"] == [] and [r.kind for r in out.result.reads] == ["query"]


def test_a_record_of_the_wrong_format_is_refused_unread(reader: AttemptReader) -> None:
    out = tools.fetch_page(_ctx(reader), {"record": "input/contributors/records.csv"})
    assert isinstance(out.result, Err) and out.result.code == "wrong_format"
    assert reader.returned == ()
    out = tools.read_table(_ctx(reader), {"record": PAGE})
    assert isinstance(out.result, Err) and out.result.code == "wrong_format"


@pytest.mark.parametrize(
    "record",
    [
        "input/competitors/../../key.json",  # out of the source
        "input/competitors/./comp01-kelvra.html",  # a second name for one record
        "input/competitors//comp01-kelvra.html",
        "competitors/comp01-kelvra.html",  # not in the input collection
    ],
)
def test_a_record_id_with_a_dot_or_empty_segment_reads_as_none(
    reader: AttemptReader, record: str
) -> None:
    out = tools.fetch_page(_ctx(reader), {"record": record})
    assert isinstance(out.result, Err) and out.result.code in ("not_an_input", "fetch_failed")
    assert reader.returned == ()


@pytest.mark.parametrize(
    "record",
    [
        "input/competitors/../reviews/reviews.json",
        "input/competitors/./comp01-kelvra.html",
        "input/../key.json",
    ],
)
def test_folder_source_reads_only_canonical_record_ids(record: str) -> None:
    source = FolderSource(tools.SOURCE, SOURCES, tools.COLLECTION)
    assert source.read(tools.SOURCE, record) is None
    assert source.read(tools.SOURCE, PAGE) is not None


def test_without_a_reader_a_fetch_fails_and_mints_nothing() -> None:
    out = tools.fetch_page(_ctx(None), {"record": PAGE})
    assert isinstance(out.result, Err) and out.result.code == "fetch_failed"
    assert out.result.item == PAGE


def test_read_table_and_read_note_draft_over_raw_offsets(reader: AttemptReader) -> None:
    reviews = tools.read_table(_ctx(reader), {"record": "input/reviews/reviews.json"})
    note = tools.read_note(_ctx(reader), {"record": "input/priorities/business-priorities.md"})
    assert isinstance(reviews.result, Ok) and isinstance(note.result, Ok)
    texts = {r.record_id: r.text for r in reader.returned}
    for out in (reviews, note):
        assert isinstance(out.result, Ok)
        for d in out.result.spans:
            assert d.end > d.start and texts[d.record_id][d.start : d.end]
    assert len(reviews.result.data["rows"]) == 42
    assert reviews.result.data["roles"]["rating"] == "rating"


def test_a_csv_export_with_its_own_column_names_reads_like_any_other(
    tmp_path: Path,
) -> None:
    (tmp_path / "x.csv").write_text(
        "ref,platform,product,posted_on,country,stars,comment\n"
        'a1,s.example,P,2026-01-02,IN,4,"Good, and cheap."\n'
        "a2,t.example,P,2026-01-03,KE,2,Too slow\n",
        encoding="utf-8",
    )
    folder = AttemptReader(FolderSource(tools.SOURCE, tmp_path, tools.COLLECTION))
    out = tools.read_table(_ctx(folder), {"record": "input/x.csv"})
    assert isinstance(out.result, Ok)
    rows = out.result.data["rows"]
    assert [(r["id"], r["site"], r["date"], r["geography"], r["text"]) for r in rows] == [
        ("a1", "s.example", "2026-01-02", "IN", "Good, and cheap."),
        ("a2", "t.example", "2026-01-03", "KE", "Too slow"),
    ]
    (returned,) = folder.returned
    for d, r in zip(out.result.spans, rows, strict=True):
        assert returned.text[d.start : d.end] == r["text"]


def test_folder_source_versions_change_with_the_bytes(tmp_path: Path) -> None:
    (tmp_path / "competitors").mkdir()
    page = tmp_path / "competitors" / "a.html"
    page.write_text("one", encoding="utf-8")
    source = FolderSource("corpus", tmp_path)
    v1 = source.record_version("o", "competitors/a.html")
    page.write_text("two", encoding="utf-8")
    assert source.record_version("o", "competitors/a.html") not in (None, v1)
    assert source.records("corpus", "competitors") == ("competitors/a.html",)
    assert source.read("other", "competitors/a.html") is None
    assert source.records("corpus", "..") == ()
