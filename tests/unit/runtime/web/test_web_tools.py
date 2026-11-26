"""The web handle and the generic web tools: a snippet is never citable, a fetched page is.

The ports are small in-test doubles, so the runtime is tested alone: ``AttemptWeb`` keeps a
fetched page as a text the attempt's reader returned, so a span drafted over it mints, and
a span over a search snippet (never returned) is refused.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from aksha.runtime.egress.client import RefusingHttpClient
from aksha.runtime.egress.ports import HttpClient
from aksha.runtime.grounding.errors import MintRefused
from aksha.runtime.grounding.reading import AttemptReader
from aksha.runtime.grounding.spans import mint_spans
from aksha.runtime.grounding.types import SpanDraft, SpanIndex
from aksha.runtime.loop.tier import web_for
from aksha.runtime.sandbox.tier import ExecutionTier
from aksha.runtime.tools.types import Err, Ok, ToolContext, ToolSpec
from aksha.runtime.web.attempt import AttemptWeb
from aksha.runtime.web.tools import fetch_url, fetch_url_spec, web_search, web_search_tool
from aksha.runtime.web.types import (
    FetchedPage,
    SearchHit,
    SearchResults,
    WebError,
    normal_url,
    text_version,
    url_problem,
    web_record_id,
)

PAGE = "<html><head><title>A probe</title></head><body><h1>A probe</h1><p>INR 4,999 per probe.</p></body></html>"  # noqa: E501


class Search:
    name = "double"

    def search(self, query: str, *, limit: int) -> SearchResults:
        hits = (SearchHit(1, "A probe", "https://a.example/probe", "INR 4,999 per probe", None),)
        return SearchResults(query, self.name, hits[:limit])


class Fetch:
    name = "double"

    def __init__(self, status: int = 200) -> None:
        self.status = status

    def fetch(self, url: str, http: HttpClient) -> FetchedPage:
        if "missing" in url:
            raise WebError("not_found", f"{url} is not on this web")
        return FetchedPage(url, url, self.status, "text/html", PAGE)


def _ctx(web: AttemptWeb | None, reader: AttemptReader) -> ToolContext:
    return ToolContext("o", "a", "t", None, "R-0001/t/t01/c01", 1, reader, web=web)


def _handle(reader: AttemptReader, status: int = 200) -> AttemptWeb:
    return AttemptWeb(Search(), Fetch(status), reader, RefusingHttpClient())


def test_a_record_id_is_the_page_s_host_and_path() -> None:
    assert web_record_id("https://A.example/probe/") == "web/a.example/probe"
    assert web_record_id("https://a.example") == "web/a.example/index"
    assert web_record_id("https://a.example/p?q=1#top") == "web/a.example/p?q=1"
    assert normal_url("HTTPS://A.example/p#x") == "https://a.example/p"


def test_a_search_returns_hits_and_mints_nothing() -> None:
    reader = AttemptReader(None)
    out = web_search(_ctx(_handle(reader), reader), {"query": "soil  probe"})
    assert isinstance(out.result, Ok) and out.result.spans == () and out.result.reads == ()
    assert out.result.data["query"] == "soil probe"
    assert out.result.data["results"][0]["url"] == "https://a.example/probe"
    assert "not evidence" in out.result.data["note"]
    assert reader.returned == ()  # a snippet is never a returned text


def test_a_fetched_page_is_kept_so_its_spans_mint() -> None:
    reader = AttemptReader(None)
    out = fetch_url(_ctx(_handle(reader), reader), {"url": "https://a.example/probe"})
    assert isinstance(out.result, Ok)
    (returned,) = reader.returned
    assert (returned.source, returned.record_id) == ("web", "web/a.example/probe")
    assert returned.version == text_version(PAGE)
    assert [str(r) for r in out.result.reads] == [f"record:web/a.example/probe@{returned.version}"]
    spec = fetch_url_spec("a")
    minted = mint_spans(out.result.spans, reader.returned, spec=spec, index=SpanIndex("o", {}),
                        run="R-0001", instance="t", step="R-0001/t/t01/c01")  # fmt: skip
    assert minted.spans and all(s.source == "web" for s in minted.spans)
    assert spec.egress == "aksha.web" and spec.mints_citation_spans


def test_a_span_over_a_snippet_is_refused() -> None:
    reader = AttemptReader(None)
    web_search(_ctx(_handle(reader), reader), {"query": "probe"})
    draft = SpanDraft("web", "web/a.example/probe", text_version(PAGE), 0, 5)
    with pytest.raises(MintRefused):
        mint_spans((draft,), reader.returned, spec=fetch_url_spec("a"), index=SpanIndex("o", {}),
                   run="R-0001", instance="t", step="s")  # fmt: skip


def test_a_failed_fetch_is_the_error_the_model_reads_and_keeps_nothing() -> None:
    for handle_status, url, code in ((200, "https://a.example/missing", "not_found"),
                                     (503, "https://a.example/probe", "fetch_failed"),
                                     (200, "ftp://a.example/x", "url_invalid"),
                                     (200, "https://a.example:port/x", "url_invalid"),
                                     (200, "https:///x", "url_invalid")):  # fmt: skip
        reader = AttemptReader(None)
        out = fetch_url(_ctx(_handle(reader, handle_status), reader), {"url": url})
        assert isinstance(out.result, Err) and out.result.code == code
        assert reader.returned == ()


def test_a_malformed_url_is_url_invalid_wherever_it_is_normalised() -> None:
    assert url_problem("https://a.example/x") is None
    assert url_problem("https://a.example:99999999999/x") is not None
    with pytest.raises(WebError) as e:
        normal_url("https://a.example:port/x")
    assert e.value.code == "url_invalid"


def test_without_a_web_port_both_tools_answer_web_off() -> None:
    reader = AttemptReader(None)
    for out in (web_search(_ctx(None, reader), {"query": "probe"}),
                fetch_url(_ctx(None, reader), {"url": "https://a.example/p"})):  # fmt: skip
        assert isinstance(out.result, Err) and out.result.code == "web_off"


def test_the_search_tool_is_a_read_under_the_web_policy_that_mints_nothing() -> None:
    spec = web_search_tool("a").spec
    assert (spec.name, spec.effect, spec.egress, spec.mints_citation_spans) == (
        "web_search", "read", "aksha.web", False,
    )  # fmt: skip


def test_only_a_tool_under_the_web_policy_is_handed_the_web() -> None:
    setup = SimpleNamespace(tier=ExecutionTier(search=Search(), web=Fetch()))
    reader, http = AttemptReader(None), RefusingHttpClient()
    for spec in (web_search_tool("a").spec, fetch_url_spec("a")):
        assert isinstance(web_for(setup, spec, reader, http), AttemptWeb)  # type: ignore[arg-type]
    reader_tool = ToolSpec("read_note", "a", "read", "Read a note.", {"type": "object"})
    other = replace(reader_tool, egress="ext.api")
    for spec in (reader_tool, other):
        assert web_for(setup, spec, reader, http) is None  # type: ignore[arg-type]
    bare = SimpleNamespace(tier=ExecutionTier())
    assert web_for(bare, fetch_url_spec("a"), reader, http) is None  # type: ignore[arg-type]
