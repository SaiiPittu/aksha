"""A world's offline web: deterministic ranking with noise mixed in, fetch by URL, hints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aksha import data
from aksha.adapters.search.world import WorldWeb, load_pages, world_hints
from aksha.runtime.egress.client import RefusingHttpClient
from aksha.runtime.web.types import WebError, web_record_id

STILLROOM = data.worlds() / "stillroom"
WORLDS = ("stillroom", "stillroom-v2", "tilldew", "cindral")


def _web() -> WorldWeb:
    return WorldWeb.of_world(STILLROOM)


def test_a_query_answers_the_same_ranked_list_every_time() -> None:
    first = _web().search("occupancy sensor", limit=10)
    again = _web().search("occupancy sensor", limit=10)
    assert first == again
    assert [h.rank for h in first.hits] == list(range(1, len(first.hits) + 1))
    assert first.provider == "world"


def test_results_mix_the_relevant_pages_with_each_kind_of_noise() -> None:
    web = _web()
    noise = {p.url: p.noise for p in web.pages}
    hits = web.search("occupancy sensor", limit=20).hits
    kinds = {noise[h.url] for h in hits}
    assert None in kinds  # relevant pages
    assert {"near_duplicate", "outdated", "spam", "irrelevant", "keyword_only"} <= kinds
    assert all(h.title and h.url.startswith("https://") for h in hits)


def test_a_title_word_outranks_a_body_word_and_ties_keep_the_declared_order() -> None:
    hits = _web().search("occupancy sensor", limit=3).hits
    # the vendor's page and its verbatim copy score the same; the original is declared first
    assert [h.url for h in hits[:2]] == [
        "https://kelvra.example/product",
        "https://sensorindex.example/listing/kelvra-ts-32",
    ]
    assert hits[0].snippet == hits[1].snippet


def test_the_snippet_is_the_page_s_shown_text_never_its_head_or_scripts() -> None:
    (hit,) = _web().search("Kelvra", limit=1).hits
    assert '"@type"' not in hit.snippet and "<" not in hit.snippet
    assert "Kelvra" in hit.snippet


def test_a_stem_matches_the_words_it_begins() -> None:
    web = WorldWeb.of_world(data.worlds() / "tilldew")
    urls = [h.url for h in web.search("salin", limit=20).hits]
    assert "https://patents.synthetic.example/SYN-PUB-0102" in urls


def test_a_query_that_shares_no_word_returns_nothing() -> None:
    assert _web().search("zebra quilting", limit=10).hits == ()


def test_a_fetch_returns_the_page_s_file_and_a_404_for_any_other_url() -> None:
    web = _web()
    page = web.fetch("https://kelvra.example/product", RefusingHttpClient())
    assert (
        page.status == 200 and page.text == (STILLROOM / "web/kelvra.example/product").read_text()
    )
    assert page.content_type.startswith("text/html")
    with pytest.raises(WebError) as e:
        web.fetch("https://kelvra.example/nothing-here", RefusingHttpClient())
    assert e.value.code == "not_found"


def test_each_page_s_file_is_its_record_id() -> None:
    for world in WORLDS:
        root = data.worlds() / world
        for p in WorldWeb.of_world(root).pages:
            assert p.file == web_record_id(p.url) and (root / p.file).is_file(), p.url


def test_every_world_declares_noise_and_hints_for_its_offline_model() -> None:
    for world in WORLDS:
        root = data.worlds() / world
        pages = WorldWeb.of_world(root).pages
        assert {p.noise for p in pages if p.noise} >= {"spam", "near_duplicate"}, world
        assert world_hints(root)["research"]["queries"], world


def test_a_malformed_declaration_is_refused() -> None:
    with pytest.raises(ValueError, match="https"):
        load_pages({"pages": [{"url": "http://a.example/x"}]})
    with pytest.raises(ValueError, match="twice"):
        load_pages({"pages": [{"url": "https://a.example/x"}, {"url": "https://a.example/x/"}]})
    with pytest.raises(ValueError, match="noise"):
        load_pages({"pages": [{"url": "https://a.example/x", "noise": "bad"}]})


def test_a_search_is_told_to_an_observer_and_reads_through_the_index(tmp_path: Path) -> None:
    (tmp_path / "web" / "a.example").mkdir(parents=True)
    (tmp_path / "web" / "a.example" / "p").write_text("<title>Probe</title><p>a probe</p>")
    (tmp_path / "web.json").write_text(json.dumps({"pages": [{"url": "https://a.example/p"}]}))
    seen: list[tuple[str, list[str]]] = []
    fetched: list[str] = []

    def read(rel: str) -> str | None:
        fetched.append(rel)
        return (tmp_path / rel).read_text()

    web = WorldWeb(load_pages(json.loads((tmp_path / "web.json").read_text())), read,
                   index=lambda rel: (tmp_path / rel).read_text(),
                   on_search=lambda q, urls: seen.append((q, urls)))  # fmt: skip
    web.search("probe", limit=5)
    assert seen == [("probe", ["https://a.example/p"])] and fetched == []
    web.fetch("https://a.example/p", RefusingHttpClient())
    assert fetched == ["web/a.example/p"]
