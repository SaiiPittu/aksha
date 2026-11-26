"""The research tools read only through the runtime's reader, and only the drafts they mint
are citable: a fetched page's passages are, a search snippet is not."""

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


def _ctx(reader: Any) -> ToolContext:
    return ToolContext("o", "research", "evidence", "k", "R-0001/evidence[k]/t01/c01", 1, reader)


@pytest.fixture
def reader() -> AttemptReader:
    return AttemptReader(FolderSource(tools.SOURCE, SOURCES))


def test_fetch_page_drafts_one_span_per_passage_over_the_returned_text(
    reader: AttemptReader,
) -> None:
    out = tools.fetch_page(_ctx(reader), {"record": "competitors/comp01-kelvra.html"})
    assert isinstance(out.result, Ok)
    (returned,) = reader.returned
    passages = out.result.data["passages"]
    assert len(out.result.spans) == len(passages)
    for draft, passage in zip(out.result.spans, passages, strict=True):
        assert (draft.source, draft.record_id, draft.version) == returned.locator()
        assert returned.text[draft.start : draft.end] == passage["text"]
    assert [str(r) for r in out.result.reads] == [
        f"record:competitors/comp01-kelvra.html@{returned.version}"
    ]
    assert returned.version.startswith("sha256:")


def test_search_mints_nothing_and_lists_what_it_read(reader: AttemptReader) -> None:
    out = tools.search(_ctx(reader), {"collection": "competitors", "terms": ["occupancy"]})
    assert isinstance(out.result, Ok)
    assert out.result.spans == ()
    data = out.result.data
    assert data["records_read"] == 6 and len(data["checked"]) == 6
    assert {h["record"] for h in data["hits"]} <= set(r.split("@")[0][7:] for r in data["checked"])
    assert "not citable" in data["note"]


def test_search_with_no_hit_still_lists_the_records_read_in_full(reader: AttemptReader) -> None:
    out = tools.search(_ctx(reader), {"collection": "competitors", "terms": ["spreadsheet"]})
    assert isinstance(out.result, Ok)
    assert out.result.data["hits"] == [] and len(out.result.reads) == 6


def test_a_record_outside_the_tool_collection_is_refused(reader: AttemptReader) -> None:
    out = tools.fetch_page(_ctx(reader), {"record": "notes/C1-self-calibration.md"})
    assert isinstance(out.result, Err) and out.result.code == "not_in_collection"
    assert reader.returned == ()


def test_a_path_out_of_the_source_reads_as_no_record(reader: AttemptReader) -> None:
    out = tools.fetch_page(_ctx(reader), {"record": "competitors/../../key.json"})
    assert isinstance(out.result, Err) and out.result.code == "fetch_failed"


def test_without_a_reader_a_fetch_fails_and_mints_nothing() -> None:
    out = tools.fetch_page(_ctx(None), {"record": "competitors/comp01-kelvra.html"})
    assert isinstance(out.result, Err) and out.result.code == "fetch_failed"
    assert out.result.item == "competitors/comp01-kelvra.html"


def test_read_reviews_and_read_note_draft_over_raw_offsets(reader: AttemptReader) -> None:
    reviews = tools.read_reviews(_ctx(reader), {"record": "reviews/reviews.json"})
    note = tools.read_note(_ctx(reader), {"record": "priorities/business-priorities.md"})
    assert isinstance(reviews.result, Ok) and isinstance(note.result, Ok)
    texts = {r.record_id: r.text for r in reader.returned}
    for out in (reviews, note):
        assert isinstance(out.result, Ok)
        for d in out.result.spans:
            assert d.end > d.start and texts[d.record_id][d.start : d.end]
    assert len(reviews.result.data["rows"]) == 42


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
