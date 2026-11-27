"""The research agent's input tools (the harness's shared ones, as the agent declares them)
read only through the runtime's reader, and only the units they mint are citable: a read
document's units are, a listing or a search hit is not. No tool expects a folder: every
input is one record of the ``input`` collection, read by format."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from aksha.adapters.sources.folder import FolderSource
from aksha.agents.research import tools
from aksha.agents.research.fakes import ADAPTERS
from aksha.runtime.grounding.reading import AttemptReader
from aksha.runtime.tools.types import Err, Ok, ToolContext
from tests.support.fast import models_for
from tests.support.research import AGENT_DIR, WORLDS

PAGE = "input/competitors/comp01-kelvra.html"
VENDORS = ("kelvra", "veyra", "tallyway", "noctra", "brightfield", "corvane")
"""The stillroom vendors, whose product pages are on the world's web."""
BODIES = {t.spec.name: t.body for t in tools.TOOLS}


@pytest.fixture(scope="module")
def uploads(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The stillroom inputs with the vendors' pages saved among them, as a person may upload.

    The read tools read any input; these tests read saved web pages as inputs, so the
    pages the world serves on its web are copied in as ``competitors/compNN-<vendor>.html``.
    """
    root = tmp_path_factory.mktemp("uploads")
    shutil.copytree(WORLDS / "stillroom" / "sources", root, dirs_exist_ok=True)
    (root / "competitors").mkdir()
    for n, vendor in enumerate(VENDORS, start=1):
        page = WORLDS / "stillroom" / "web" / f"{vendor}.example" / "product"
        shutil.copyfile(page, root / "competitors" / f"comp0{n}-{vendor}.html")
    return root


def _ctx(reader: Any, tool: str = "read_document") -> ToolContext:
    """The context the loop gives ``tool``'s body in fake mode, its fast models bound."""
    return ToolContext("o", "research", "evidence", "k", "R-0001/evidence[k]/t01/c01", 1, reader,
                       models=models_for(AGENT_DIR, ADAPTERS, tool))  # fmt: skip


def call(name: str, ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = BODIES[name]
    assert body is not None
    return body(ctx, args)


@pytest.fixture
def reader(uploads: Path) -> AttemptReader:
    return AttemptReader(FolderSource(tools.SOURCE, uploads, tools.COLLECTION))


def test_the_agent_lists_only_shared_tools() -> None:
    assert sorted(BODIES) == ["fetch_url", "list_inputs", "read_document", "search_inputs",
                              "todo", "web_search"]  # fmt: skip


def test_read_document_drafts_one_span_per_unit_over_the_returned_text(
    reader: AttemptReader,
) -> None:
    out = call("read_document", _ctx(reader), {"ref": PAGE, "extract": True})
    assert isinstance(out.result, Ok)
    (returned,) = reader.returned
    units = out.result.data["units"]
    assert len(out.result.spans) == len(units)
    for draft, unit in zip(out.result.spans, units, strict=True):
        assert (draft.source, draft.record_id, draft.version) == returned.locator()
        assert (draft.start, draft.end) == (unit[4], unit[5])
    assert out.result.data["kind"] == "competitor_page"
    prices = out.result.data["fields"]["prices"]
    assert prices["value"] and len(prices["units"]) == len(prices["value"])
    for n in prices["units"]:  # each price is a unit of its own, so it can be cited whole
        quoted = returned.text[units[n][4] : units[n][5]]
        assert any(c.isdigit() for c in quoted)
    assert [str(r) for r in out.result.reads] == [f"record:{PAGE}@{returned.version}"]
    assert returned.version.startswith("sha256:")


def test_list_inputs_finds_pages_and_review_tables_by_content_not_by_folder(
    reader: AttemptReader, uploads: Path
) -> None:
    out = call("list_inputs", _ctx(reader, "list_inputs"), {})
    assert isinstance(out.result, Ok) and out.result.spans == ()
    kinds = {r["ref"]: r["kind"] for r in out.result.data["inputs"]}
    assert len(kinds) == sum(1 for p in uploads.rglob("*") if p.is_file())
    sellers = sorted(r for r, s in kinds.items() if s == "competitor_page")
    assert sellers == sorted(
        f"input/competitors/{p.name}" for p in (uploads / "competitors").iterdir()
    )
    assert [r for r, s in kinds.items() if s == "customer_reviews"] == [
        "input/reviews/reviews.json"
    ]
    assert kinds["input/contributors/records.csv"] == "other"  # text, but no rating: not reviews
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
    args = {"kinds": ["competitor_page"]}

    def version() -> str:
        out = call("list_inputs", _ctx(folder, "list_inputs"), args)
        assert isinstance(out.result, Ok)
        return str(out.result.reads[0].version)

    first = version()
    (tmp_path / "note.md").write_text("# A note\n\nNothing for sale.\n", encoding="utf-8")
    assert version() == first  # a new note is no new seller page: the listing did not move
    (tmp_path / "b.md").write_text("---\nproduct: B\n---\n# B\n", encoding="utf-8")
    assert version() != first


def test_search_mints_nothing_and_records_only_its_hit_set(reader: AttemptReader) -> None:
    out = call("search_inputs", _ctx(reader, "search_inputs"),
               {"terms": ["occupancy"], "formats": ["html"]})  # fmt: skip
    assert isinstance(out.result, Ok)
    assert out.result.spans == ()
    data = out.result.data
    assert data["inputs_searched"] == 6 and data["hits"]
    assert "not citable" in data["note"]
    (read,) = out.result.reads
    assert read.kind == "query" and data["recorded"] == f"query:{read.id}@{read.version}"


def test_search_with_no_hit_still_records_its_query(reader: AttemptReader) -> None:
    out = call("search_inputs", _ctx(reader, "search_inputs"),
               {"terms": ["spreadsheet"], "formats": ["html"]})  # fmt: skip
    assert isinstance(out.result, Ok)
    assert out.result.data["hits"] == [] and [r.kind for r in out.result.reads] == ["query"]


def test_any_record_reads_whatever_its_format(reader: AttemptReader) -> None:
    # no format is refused: a table is its rows, each citable, and the decide step says
    # what kind it is (here ``other``, left to the general model)
    out = call("read_document", _ctx(reader), {"ref": "input/contributors/records.csv",
                                               "extract": True})  # fmt: skip
    assert isinstance(out.result, Ok)
    assert (out.result.data["kind"], out.result.data["unresolved"]) == ("other", ["kind"])
    assert out.result.spans
    out = call("read_document", _ctx(reader), {"ref": "input/reviews/reviews.json"})
    assert isinstance(out.result, Ok)


@pytest.mark.parametrize(
    "record",
    [
        "input/competitors/../../key.json",  # out of the source
        "input/competitors/./comp01-kelvra.html",  # a second name for one record
        "input/competitors//comp01-kelvra.html",
        "competitors/x/comp01-kelvra.html",  # no input of that name
    ],
)
def test_a_record_id_with_a_dot_or_empty_segment_reads_as_none(
    reader: AttemptReader, record: str
) -> None:
    out = call("read_document", _ctx(reader), {"ref": record})
    assert isinstance(out.result, Err)
    assert out.result.code in ("not_an_input", "not_found")
    assert reader.returned == ()


@pytest.mark.parametrize(
    "record",
    [
        "input/competitors/../reviews/reviews.json",
        "input/competitors/./comp01-kelvra.html",
        "input/../key.json",
    ],
)
def test_folder_source_reads_only_canonical_record_ids(record: str, uploads: Path) -> None:
    source = FolderSource(tools.SOURCE, uploads, tools.COLLECTION)
    assert source.read(tools.SOURCE, record) is None
    assert source.read(tools.SOURCE, PAGE) is not None


def test_without_a_reader_a_read_fails_and_mints_nothing() -> None:
    out = call("read_document", _ctx(None), {"ref": PAGE})
    assert isinstance(out.result, Err) and out.result.code == "not_found"
    assert out.result.item == PAGE


def test_units_are_exact_raw_ranges(reader: AttemptReader) -> None:
    reviews = call("read_document", _ctx(reader), {"ref": "input/reviews/reviews.json"})
    note = call("read_document", _ctx(reader), {"ref": "input/priorities/business-priorities.md"})
    assert isinstance(reviews.result, Ok) and isinstance(note.result, Ok)
    texts = {r.record_id: r.text for r in reader.returned}
    for out in (reviews, note):
        assert isinstance(out.result, Ok)
        for d in out.result.spans:
            assert d.end > d.start and texts[d.record_id][d.start : d.end]
    rows = [u for u in reviews.result.data["units"] if u[1] == "row"]
    assert len(rows) == 42


def test_a_csv_export_with_its_own_column_names_reads_like_any_other(tmp_path: Path) -> None:
    (tmp_path / "x.csv").write_text(
        "ref,platform,product,posted_on,country,stars,comment\n"
        'a1,s.example,P,2026-01-02,IN,4,"Good, and cheap."\n'
        "a2,t.example,P,2026-01-03,KE,2,Too slow\n",
        encoding="utf-8",
    )
    folder = AttemptReader(FolderSource(tools.SOURCE, tmp_path, tools.COLLECTION))
    out = call("read_document", _ctx(folder), {"ref": "input/x.csv", "cells": True})
    assert isinstance(out.result, Ok)
    (returned,) = folder.returned
    cells = [returned.text[d.start : d.end]
             for d, u in zip(out.result.spans, out.result.data["units"], strict=True)
             if u[1] == "cell"]  # fmt: skip
    assert "Good, and cheap." in cells and "Too slow" in cells and "KE" in cells


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
