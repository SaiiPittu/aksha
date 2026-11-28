"""Scoped claims (ADR-P13 amendment M1): an absence or coverage claim names its scope.

A claim that something is not there, or a count over a whole, rests on no words: it rests
on what was looked at. The save path refuses one that names no scope, a malformed one and
one whose scope the instance never read; a well-formed one adds no citation. A span whose
record moved but whose text a recheck found unchanged stays citable.
"""

from __future__ import annotations

from typing import Any

import pytest

from aksha.runtime.grounding.claims import Scope, claims_in, parse_claim, record_id
from aksha.runtime.grounding.types import CitationSpan, CiteError, CitePolicy, SpanCite, SpanIndex
from aksha.runtime.grounding.verify import Evidence, verify_citation, verify_content

RUN, ME = "R-0001", "survey"
QUERY = 'syn.search?{"term":"lidar"}'
READS = frozenset({f"query:{QUERY}@q-1", "record:pages/P-0001@2", "record:pages/P-0002@1"})


def evidence(**kw: Any) -> Evidence:
    fields: dict[str, Any] = {
        "spans": SpanIndex("o1", {}),
        "run": RUN,
        "instance": ME,
        "records": {"pages/P-0001": "2", "pages/P-0002": "3"},
        "reads": READS,
        "artifact": lambda ref: None,
    }
    fields.update(kw)
    return Evidence(**fields)


def absence(scope: Any = None, **extra: Any) -> dict[str, Any]:
    inner: dict[str, Any] = {"statement": "no page mentions a lidar", **extra}
    if scope is not None:
        inner["scope"] = scope
    return {"absence": inner}


def codes(content: dict[str, Any], policies: tuple[CitePolicy, ...] = ()) -> list[str]:
    return [
        r.code for r in verify_content(content, policies, evidence()) if isinstance(r, CiteError)
    ]


def test_an_absence_claim_scoped_to_a_search_it_ran_is_accepted() -> None:
    assert verify_content({"no_lidar": absence({"query": QUERY})}, (), evidence()) == ()
    (claim,) = claims_in({"no_lidar": absence({"query": QUERY})})
    assert claim.form == "absence" and claim.pointer == "/no_lidar"
    assert claim.scope == Scope((QUERY,), ())


def test_a_scope_may_name_several_searches_each_as_its_id_or_its_recorded_read() -> None:
    """An empty market rests on every search that found nothing: a list, in either form."""
    other = 'syn.search?{"term":"radar"}'
    reads = READS | {f"query:{other}@q-2"}
    both = absence({"query": [f"query:{QUERY}@q-1", other]})
    assert verify_content({"none": both}, (), evidence(reads=reads)) == ()
    (claim,) = claims_in({"none": both})
    assert claim.scope == Scope((QUERY, other), ())
    unread = absence({"query": [QUERY, 'syn.search?{"term":"sonar"}']})
    assert codes({"none": unread}) == ["scope_not_read"]
    for bad in ([], [""], [3]):
        assert codes({"none": absence({"query": bad})}) == ["scope_invalid"], bad


def test_a_claim_with_no_scope_is_refused_with_what_to_add() -> None:
    (refused,) = verify_content({"no_lidar": absence()}, (), evidence())
    assert isinstance(refused, CiteError)
    assert (refused.code, refused.pointer) == ("scope_missing", "/no_lidar")
    assert '"scope": {"query"' in refused.message and "new matching source" in refused.message
    assert codes({"x": {"absence": "no page mentions a lidar"}}) == ["scope_missing"]


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"query": ""},
        {"records": ["pages/P-0001"]},
        {"query": QUERY, "where": "x"},
        {"query": QUERY, "hits": ["pages/P-0001"]},
        "all",
    ],
)
def test_a_malformed_scope_is_refused(scope: Any) -> None:
    assert codes({"x": absence(scope)}) == ["scope_invalid"]


def test_a_scope_the_instance_never_read_or_that_moved_is_refused() -> None:
    assert codes({"x": absence({"query": "syn.search?{}"})}) == ["scope_not_read"]
    assert codes({"x": absence({"records": ["record:pages/P-0009@1"]})}) == ["scope_not_read"]
    assert codes({"x": absence({"records": ["record:pages/P-0002@1"]})}) == ["scope_not_read"]
    assert codes({"x": absence({"records": ["record:pages/P-0001@2"]})}) == []


def test_a_scoped_query_must_match_a_read_query_exactly() -> None:
    # A query ID may hold "@" in its serialized arguments. A scope that stops just before
    # it names a search the instance never ran, though the read text starts with it, and
    # invalidation compares the scope's ID exactly, so the claim would never reopen.
    full = 'syn.search?{"term":"desk@floor"}'
    shorter = 'syn.search?{"term":"desk'
    ev = evidence(reads=frozenset({*READS, f"query:{full}@q-2"}))
    assert verify_content({"x": absence({"query": full})}, (), ev) == ()
    (refused,) = verify_content({"x": absence({"query": shorter})}, (), ev)
    assert isinstance(refused, CiteError) and refused.code == "scope_not_read"


def test_a_coverage_claim_states_its_count_and_is_checked_in_a_declared_field() -> None:
    good = {"coverage": {"statement": "pages that name a price", "count": 2, "of": 5,
                         "scope": {"query": QUERY}}}  # fmt: skip
    policy = (CitePolicy("/counts/*"),)
    assert codes({"counts": [good]}, policy) == []  # a scoped claim grounds a declared field
    no_count = {"coverage": {"statement": "pages that name a price", "scope": {"query": QUERY}}}
    assert codes({"counts": [no_count]}, policy) == ["scope_invalid"]
    assert codes({"counts": [{"coverage": {"statement": "s", "count": 1}}]}, policy) == [
        "scope_missing"
    ]


def test_parse_and_record_ids() -> None:
    claim = parse_claim(absence({"records": ["record:input/a.csv#where:id=R1@p-2"]}), "/x")
    assert not isinstance(claim, CiteError)
    assert claim.scope.record_ids() == frozenset({"input/a.csv"})  # type: ignore[union-attr]
    assert record_id("record:pages/P-0001@2") == "pages/P-0001"


def test_a_span_whose_record_moved_stays_citable_while_its_text_is_kept() -> None:
    span = CitationSpan("EX-0001", "o1", "docs", "pages/P-0001", "1", 0, 30,
                        "The sensor uses no camera at all.", "read",
                        shown_to=frozenset({(RUN, ME)}))  # fmt: skip
    ev = evidence(spans=SpanIndex("o1", {"EX-0001": span}))
    cite = SpanCite("EX-0001", "The sensor uses no camera at all.")
    stale = verify_citation(cite, ev)
    assert isinstance(stale, CiteError) and stale.code == "stale_source"
    kept = verify_citation(cite, evidence(spans=ev.spans, kept_spans=frozenset({"EX-0001"})))
    assert not isinstance(kept, CiteError)
