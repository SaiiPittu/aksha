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
    assert [c.key for c in companies] == ["brightfield-analytics", "kelvra-systems"]
    merged = companies[0]
    assert merged.canonical == "Brightfield Analytics"  # the earliest page's company
    assert merged.aliases == ("OccuPoint", "Corvane Group", "Presence P1")
    assert [(a.record, b.record) for a, b, _ in merged.merges] == [("p/b", "p/a")]


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


def test_price_conflict_needs_two_different_unit_prices() -> None:
    p229 = {"currency": "USD", "value": "229", "per": "unit"}
    p199 = {"currency": "USD", "value": "199", "per": "unit"}
    monthly = {"currency": "USD", "value": "7", "per": "sensor per month"}
    assert analysis.price_conflict([p229, p199])
    assert not analysis.price_conflict([p229, dict(p229), monthly])


def test_claim_labels() -> None:
    assert analysis.claim_label("claim", "Up to 7 years of battery life.") == "vendor_claim"
    assert (
        analysis.claim_label("text", "Battery powered, 5 year battery life claimed.")
        == "vendor_claim"
    )
    assert analysis.claim_label("spec", "14 months at 1 report per minute") == "fact"


def _row(rid: str, date: str, product: str, text: str, geo: str = "DE") -> dict[str, Any]:
    return {
        "fields": {"review_id": rid, "date": date, "product": product, "geography": geo},
        "text": text,
    }


def test_a_cross_posted_review_is_counted_once_and_its_theme_too() -> None:
    rows = [
        _row("R-1", "2026-02-27", "Beam", "We replace batteries every 14 months."),
        _row("R-2", "2026-03-02", "Beam", "We replace  batteries every 14 months."),
        _row(
            "R-3", "2026-03-03", "Halo", "We replace batteries every 14 months."
        ),  # another product
        _row("R-4", "2026-03-04", "Halo", "Cheap and battery powered."),
    ]
    sample = analysis.review_sample(rows)
    assert sample.duplicates == (("R-2", "R-1"),)
    assert len(sample.unique) == 3
    assert [r["fields"]["review_id"] for r in sample.themes["battery"]] == ["R-1", "R-3"]
    assert sample.phrase("battery") == "2 of 3 reviews in this sample"


def test_stillness_and_privacy_rules() -> None:
    rows = [
        _row("R-1", "2026-01-01", "A", "The lights went off while I was sitting alone."),
        _row(
            "R-2", "2026-01-02", "A", "Works council asked for written proof that no image leaves."
        ),
        _row("R-3", "2026-01-03", "A", "Good value."),
    ]
    sample = analysis.review_sample(rows)
    assert [r["fields"]["review_id"] for r in sample.themes["stillness"]] == ["R-1"]
    assert [r["fields"]["review_id"] for r in sample.themes["privacy"]] == ["R-2"]
    assert analysis.tally(sample.unique, "geography") == {"DE": 3}


def test_slug_is_a_valid_instance_key() -> None:
    assert analysis.slug("Brightfield Analytics") == "brightfield-analytics"
    assert analysis.slug("A/B [x]") == "a-b-x"
