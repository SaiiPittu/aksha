"""The research agent's rules: entity resolution, price conflicts, review samples, labels."""

from __future__ import annotations

from typing import Any

from aksha.agents.research import analysis
from aksha.agents.research.analysis import PageIdentity


def _page(record: str, published: str, product: str, brand: str, *rel: str) -> PageIdentity:
    relations = tuple({"text": t, "span": f"EX-{i}"} for i, t in enumerate(rel))
    return PageIdentity(record, "v1", published, product, brand, relations=relations)


def test_pages_merge_only_on_a_relationship_sentence() -> None:
    old = _page("p/a", "2026-04-02", "OccuPoint", "Brightfield Analytics")
    said = "Presence P1 is the new name for OccuPoint. Brightfield Analytics joined Corvane Group."
    new = _page("p/b", "2026-09-15", "Presence P1", "Corvane Group", said)
    other = _page("p/c", "2026-01-01", "Kelvra TS-32", "Kelvra Systems")
    companies = analysis.resolve([old, new, other])
    # In the order their first page came, never by name: a renamed world keeps the order.
    assert [c.key for c in companies] == ["c01-brightfield-analytics", "c02-kelvra-systems"]
    merged = companies[0]
    assert merged.canonical == "Brightfield Analytics"  # the earliest page's company
    assert merged.aliases == ("OccuPoint", "Corvane Group", "Presence P1")
    assert [(a.record, b.record) for a, b, _ in merged.merges] == [("p/b", "p/a")]


def test_two_sections_of_one_record_are_two_pages_and_merge_only_on_a_sentence() -> None:
    """A memo with a section per seller: one record, two pages, two companies."""
    a = PageIdentity("memo.md", "v1", "", "FW-200", "Firewise", section=1)
    b = PageIdentity("memo.md", "v1", "", "KM Pro", "Kilnmate", section=2)
    assert [c.canonical for c in analysis.resolve([a, b])] == ["Firewise", "Kilnmate"]
    said = ({"text": "Kilnmate was acquired by Firewise.", "span": "EX-1"},)
    b2 = PageIdentity("memo.md", "v1", "", "KM Pro", "Kilnmate", relations=said, section=2)
    (merged,) = analysis.resolve([a, b2])
    assert [p.section for p in merged.pages] == [1, 2]


def test_companies_come_in_page_discovery_order_whatever_their_names() -> None:
    """A rename must not reorder the companies: their order is their first page's position."""
    pages = [
        _page("p/01", "2026-01-01", "Zeta One", "Zeta Labs"),
        _page("p/02", "2026-01-01", "Alpha One", "Alpha Labs"),
        _page("p/03", "2026-01-01", "Mid One", "Mid Labs"),
    ]
    renamed = [
        _page("p/01", "2026-01-01", "Able One", "Able Labs"),
        _page("p/02", "2026-01-01", "Yonder One", "Yonder Labs"),
        _page("p/03", "2026-01-01", "Bravo One", "Bravo Labs"),
    ]
    order = [[p.record for c in analysis.resolve(ps) for p in c.pages] for ps in (pages, renamed)]
    assert order[0] == order[1] == ["p/01", "p/02", "p/03"]
    keys = [c.key for c in analysis.resolve(renamed)]
    assert keys == sorted(keys)  # the runtime fans out by sorted key, so keys sort that way too


def test_names_that_look_alike_are_never_merged() -> None:
    a = _page("p/a", "2026-01-01", "Kelvra TS-32", "Kelvra Systems")
    b = _page(
        "p/b",
        "2026-02-01",
        "Kelvra TS-32 Pro",
        "Kelvra Systemz",
        "Kelvra Systemz is a new company.",
    )
    assert len(analysis.resolve([a, b])) == 2


def test_a_relationship_word_without_another_page_name_merges_nothing() -> None:
    a = _page("p/a", "2026-01-01", "Halo", "Veyra Labs", "Veyra Labs joined a trade group.")
    b = _page("p/b", "2026-01-01", "Beam", "Tallyway")
    assert len(analysis.resolve([a, b])) == 2


def test_price_conflict_needs_two_pages_each_stating_one_differing_price() -> None:
    p229 = {"record": "a", "currency": "USD", "value": "229", "per": "unit"}
    p199 = {"record": "b", "currency": "USD", "value": "199", "per": "unit"}
    monthly = {"record": "b", "currency": "USD", "value": "7", "per": "sensor per month"}
    same = {"record": "b", "currency": "USD", "value": "7", "per": "Sensor  per month"}
    assert analysis.price_conflict([p229, p199, monthly]) == ["unit"]
    assert analysis.price_conflict([p229, dict(p229, record="b"), monthly, same]) == []
    # Two prices on one page on one basis are tiers, not a conflict.
    tier = {"record": "a", "currency": "USD", "value": "249", "per": "unit"}
    assert analysis.price_conflict([p229, tier, p199]) == []


def test_terms_match_words_prefixes_and_exclusions() -> None:
    terms = ["batter*", "not battery powered", "AA cells"]
    assert analysis.term_hits("Batteries died after a year.", terms) == ["batter*"]
    assert analysis.term_hits("Cheap and battery powered.", terms) == []
    assert analysis.term_hits("Battery powered, and the battery died.", terms) == ["batter*"]
    assert analysis.term_hits("We change the AA  cells often.", terms) == ["AA cells"]
    assert analysis.term_hits("A cellar.", ["cell"]) == []


def test_claim_labels() -> None:
    assert analysis.claim_label("claim", "Up to 7 years of battery life.") == "vendor_claim"
    assert (
        analysis.claim_label("text", "Battery powered, 5 year battery life claimed.")
        == "vendor_claim"
    )
    assert analysis.claim_label("spec", "14 months at 1 report per minute") == "fact"


def _row(rid: str, date: str, product: str, text: str, geo: str = "DE") -> dict[str, Any]:
    return {
        "fields": {"id": rid, "date": date, "product": product, "geography": geo},
        "text": text,
    }


def test_a_cross_posted_review_is_counted_once_and_its_theme_too() -> None:
    rows = [
        _row("R-1", "2026-02-27", "Beam", "We replace batteries every 14 months."),
        _row("R-2", "2026-03-02", "Beam", "We replace  batteries every 14 months."),
        _row(
            "R-3", "2026-03-03", "Halo", "We replace batteries every 14 months."
        ),  # the same words under another listing: a syndicated copy
        _row("R-4", "2026-03-04", "Halo", "Cheap and battery powered."),
    ]
    sample = analysis.review_sample(rows, {"battery": ["batter*", "not battery powered"]})
    assert sample.duplicates == (("R-2", "R-1"), ("R-3", "R-1"))
    assert len(sample.unique) == 2
    assert [r["fields"]["id"] for r in sample.themes["battery"]] == ["R-1"]
    assert sample.phrase("battery") == "1 of 2 reviews in this sample"


def test_a_repost_is_found_after_a_rename_or_behind_a_lead_in() -> None:
    long = "the overage line on our invoice doubled in June, which nobody had planned for."
    rows = [
        _row("R-1", "2026-01-01", "Oldname Pro", "Oldname Pro is good for drills, but slow."),
        _row("R-2", "2026-05-01", "Newname", "Newname is good for drills, but slow."),
        _row("R-3", "2026-01-02", "Tool", long.capitalize()),
        _row("R-4", "2026-02-02", "Tool", "Cross-posting from the other thread: " + long),
        _row("R-5", "2026-02-03", "Tool", "Slow."),
        _row("R-6", "2026-02-04", "Tool", "It is slow."),  # too short to be a repost of R-5
    ]
    sample = analysis.review_sample(rows, {})
    assert sample.duplicates == (("R-2", "R-1"), ("R-4", "R-3"))


def test_a_short_common_phrase_under_another_product_is_its_own_review() -> None:
    rows = [
        _row("R-1", "2026-01-01", "Beam", "Works well."),
        _row("R-2", "2026-01-02", "Halo", "Works well."),
        _row("R-3", "2026-01-03", "Beam", "Works well."),  # the same product: a copy
    ]
    sample = analysis.review_sample(rows, {})
    assert sample.duplicates == (("R-3", "R-1"),)
    assert len(sample.unique) == 2


def test_themes_are_the_requests_own_terms() -> None:
    rows = [
        _row("R-1", "2026-01-01", "A", "The lights went off while I was sitting alone."),
        _row(
            "R-2", "2026-01-02", "A", "Works council asked for written proof that no image leaves."
        ),
        _row("R-3", "2026-01-03", "A", "Good value."),
    ]
    themes = {"stillness": ["sit*", "still"], "privacy": ["privacy", "works council", "image"]}
    sample = analysis.review_sample(rows, themes)
    assert [r["fields"]["id"] for r in sample.themes["stillness"]] == ["R-1"]
    assert [r["fields"]["id"] for r in sample.themes["privacy"]] == ["R-2"]
    assert analysis.tally(sample.unique, "geography") == {"DE": 3}


def test_slug_is_a_valid_instance_key() -> None:
    assert analysis.slug("Brightfield Analytics") == "brightfield-analytics"
    assert analysis.slug("A/B [x]") == "a-b-x"


def test_a_rename_stated_elsewhere_merges_only_the_names_in_its_sentence() -> None:
    from aksha.agents.research.fakes import Result, _statements

    passage = "Glimmer and Dovetail both cut prices. Oldmark is now called Newmark."
    data = {"record": "news", "version": "v1", "passages": [{"n": 0, "text": passage}]}
    said = _statements(Result("R-1/x", "read", {}, data, None, ("EX-0001",)))
    assert [s["text"] for s in said] == ["Oldmark is now called Newmark."]
    pages = [
        analysis.PageIdentity(n.lower(), "v1", "2026-01-01", n, n)
        for n in ("Glimmer", "Dovetail", "Oldmark", "Newmark")
    ]
    merged = [c for c in analysis.resolve(pages, [said]) if len(c.pages) > 1]
    assert [sorted(p.product for p in c.pages) for c in merged] == [["Newmark", "Oldmark"]]
