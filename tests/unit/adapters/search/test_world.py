"""A world's offline web: deterministic ranking with noise mixed in, fetch by URL, hints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aksha import data
from aksha.adapters.search.world import (
    WorldWeb,
    load_pages,
    world_hints,
    world_inputs_environment,
)
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
    for url in ("https://a.example/../../key.json", "https://../key.json",
                "https://a.example/./x"):  # fmt: skip
        with pytest.raises(ValueError, match="segment"):
            load_pages({"pages": [{"url": url}]})


def test_a_world_s_web_reads_only_files_under_its_web_folder(tmp_path: Path) -> None:
    (tmp_path / "web" / "a.example").mkdir(parents=True)
    (tmp_path / "web" / "a.example" / "p").write_text("<p>a probe</p>")
    (tmp_path / "key.json").write_text('{"answer": "secret"}')
    (tmp_path / "web.json").write_text(json.dumps({"pages": [{"url": "https://a.example/p"}]}))
    web = WorldWeb.of_world(tmp_path)
    assert web._read("web/a.example/p") == "<p>a probe</p>"
    assert web._read("key.json") is None and web._read("web/../key.json") is None


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


def test_every_world_web_frames_its_synthetic_pages_as_the_exercise_s_market() -> None:
    """The pages keep their synthetic label; the web itself says what that label means here.

    A run that read only the label left every seller out and reported an empty market, so
    the note travels with every result and every fetched page, from the world, never from
    an agent's prompt.
    """
    for world in WORLDS:
        web = WorldWeb.of_world(data.worlds() / world)
        note = web.environment
        assert "synthetic market" in note and "are the market" in note, world
        assert "not as a reason to leave the page out" in note, world
        assert "—" not in note
        url = web.pages[0].url
        found = web.search(url.split("/")[2].split(".")[0], limit=5)
        assert found.hits, world
        assert found.environment == note and found.as_json()["environment"] == note, world
        page = web.fetch(url, RefusingHttpClient())
        assert page.environment == note, world
        assert "synthetic" in page.text.lower()  # the page's own label is kept


def test_a_web_with_no_environment_note_adds_nothing_to_its_results() -> None:
    web = WorldWeb(_web().pages, _web()._read)
    found = web.search("occupancy sensor", limit=3)
    assert found.environment == "" and "environment" not in found.as_json()
    assert "environment" not in web.fetch("https://kelvra.example/product",
                                          RefusingHttpClient()).as_json()  # fmt: skip


RESEARCH_WORLDS = (*WORLDS, "kelasora", "vantlow", "rimefold")
"""Every world whose records a research comparison reads, with a web or inputs only."""


def test_every_research_world_frames_its_supplied_records_and_declares_its_labels() -> None:
    """The records keep their label; the world's manifest says what it means in the exercise.

    The scope and voice tasks read only inputs, so the web's note never reaches them: the
    world's own note on its records does, through the input tools. The labels it declares
    are what the graders read (``research.exclusion_not_on_label``).
    """
    for world in RESEARCH_WORLDS:
        root = data.worlds() / world
        note = world_inputs_environment(root)
        assert "synthetic sample" in note and "safety label" in note, world
        assert "not evidence that a record sells nothing" in note, world
        assert "\u2014" not in note
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["label"], world
        texts = [f.read_text(encoding="utf-8", errors="ignore")
                 for f in (root / "sources").rglob("*") if f.is_file()]  # fmt: skip
        for label in manifest["label"]:
            assert any(label in t for t in texts), (world, label)
    for world in WORLDS:
        web = json.loads((data.worlds() / world / "web.json").read_text(encoding="utf-8"))
        assert "not evidence that a page sells nothing" in web["environment"], world
        assert web["label"], world
    assert world_inputs_environment(data.worlds() / "brackwen") == ""
