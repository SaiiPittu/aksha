"""The research agent's completion checks fail on what they guard, and pass otherwise."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aksha.agents.research import checks


@dataclass
class _Version:
    content: Mapping[str, Any]


@dataclass
class _Event:
    type: str
    payload: Mapping[str, Any]


@dataclass
class _Log:
    events: list[_Event] = field(default_factory=list)


@dataclass
class _View:
    """The parts of a ``TaskView`` the checks read."""

    slots: dict[tuple[str, str | None], Mapping[str, Any]]
    key: str | None = None
    log: _Log = field(default_factory=_Log)

    def latest(self, kind: str, key: str | None = None) -> _Version | None:
        found = self.slots.get((kind, key))
        return None if found is None else _Version(found)


def _company(key: str, *pages: str, evidence: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"key": key, "pages": [{"record": p} for p in pages]}
    out["merge_evidence"] = (
        [{"cite": {"span": "EX-1", "quote": "x is the new name for y"}}] if evidence else []
    )
    return out


BOUNDARY = {"in": "products that do the job", "out": []}
DIMENSIONS = [{"id": "power", "name": "power", "terms": ["PoE"]}]
THEMES = [{"id": "battery", "name": "battery", "terms": ["batter*"]}]


def _scope(geography: str, *, basis: str = "answer", **extra: Any) -> dict[str, Any]:
    return {
        "segment": {"value": "offices", "basis": "request"},
        "geography": {"value": geography, "basis": basis},
        "boundary": BOUNDARY,
        "dimensions": DIMENSIONS,
        "themes": THEMES,
        "plan": {"kind": "comparison"},
        **extra,
    }


def test_scope_needs_segment_and_geography() -> None:
    view = _View({("scope", None): _scope("")})
    assert not checks.scope_done(view).ok
    view.slots[("scope", None)] = _scope("EU")
    assert checks.scope_done(view).ok


def test_a_synthesis_needs_no_segment_or_geography_but_names_its_themes() -> None:
    plan = {"kind": "synthesis"}
    assert checks.scope_done(_View({("scope", None): _scope("", plan=plan, segment={})})).ok
    no_themes = _scope("", plan=plan, themes=[], segment={})
    assert not checks.scope_done(_View({("scope", None): no_themes})).ok
    assert not checks.scope_done(_View({("scope", None): _scope("EU", plan={})})).ok


def test_scope_needs_where_each_value_came_from_and_a_boundary() -> None:
    assert not checks.scope_done(_View({("scope", None): _scope("EU", basis="")})).ok
    assert not checks.scope_done(_View({("scope", None): _scope("EU", boundary={})})).ok


def test_scope_needs_what_to_compare_but_no_keyword_terms() -> None:
    assert checks.scope_done(_View({("scope", None): _scope("EU", dimensions=[], themes=[])})).ok
    # a live model judges a passage against a dimension by meaning (theme_match), so terms
    # are a hint, never required (audit item 20)
    no_terms = [{"id": "power", "name": "power", "terms": []}]
    assert checks.scope_done(_View({("scope", None): _scope("EU", dimensions=no_terms)})).ok
    twice = DIMENSIONS * 2
    assert not checks.scope_done(_View({("scope", None): _scope("EU", dimensions=twice)})).ok
    missing = {k: v for k, v in _scope("EU").items() if k != "themes"}
    assert not checks.scope_done(_View({("scope", None): missing})).ok


def _link(src: str, dst: str) -> _Event:
    payload = {"kind": "research.same_entity_as", "src": {"id": src}, "dst": {"id": dst}}
    return _Event("relation.saved", payload)


def test_discover_refuses_a_merge_without_its_relation() -> None:
    content = {"companies": [_company("a", "p/1", "p/2", evidence=True), _company("b", "p/3")],
               "resolved": 2, "pages_found": 3}  # fmt: skip
    view = _View({("competitors", None): content})
    assert "same_entity_as" in checks.discover_done(view).message
    view.log.events.append(_link("p/1", "p/2"))
    assert checks.discover_done(view).ok


def test_discover_refuses_a_relation_that_joins_other_pages() -> None:
    content = {"companies": [_company("a", "p/1", "p/2", evidence=True), _company("b", "p/3")],
               "resolved": 2, "pages_found": 3}  # fmt: skip
    view = _View({("competitors", None): content})
    view.log.events.append(_link("p/1", "p/3"))  # one relation, but not between a's pages
    assert checks.discover_done(view).keys == ("a",)


def test_discover_refuses_a_page_under_two_companies_and_a_wrong_count() -> None:
    twice = {
        "companies": [_company("a", "p/1"), _company("b", "p/1")],
        "resolved": 2,
        "pages_found": 2,
    }
    assert not checks.discover_done(_View({("competitors", None): twice})).ok
    miscount = {
        "companies": [_company("a", "p/1"), _company("b", "p/2")],
        "resolved": 3,
        "pages_found": 2,
    }
    assert not checks.discover_done(_View({("competitors", None): miscount})).ok


def test_discover_takes_two_sections_of_one_record_as_two_pages() -> None:
    """A memo with one section per seller: each section is its own company's page."""
    a, b = _company("a", "memo.md"), _company("b", "memo.md")
    a["pages"][0]["section"], b["pages"][0]["section"] = 1, 2
    content = {"companies": [a, b], "resolved": 2, "pages_found": 2}
    assert checks.discover_done(_View({("competitors", None): content})).ok
    b["pages"][0]["section"] = 1
    assert not checks.discover_done(_View({("competitors", None): content})).ok


def _profile(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"prices": [{"value": "x", "record": "p/1", "per": "unit"}],
                            "cells": {"power": [{"value": "x"}]}, "conflicts": []}  # fmt: skip
    return {**base, **over}


def _slots(profile: Mapping[str, Any]) -> dict[tuple[str, str | None], Mapping[str, Any]]:
    # The competitors carry the scope's dimensions on to each company's evidence.
    return {("profile", "k"): profile, ("competitors", None): {"dimensions": DIMENSIONS}}


def _price(value: str, record: str, currency: str = "USD", per: str = "unit") -> dict[str, Any]:
    return {"value": value, "record": record, "currency": currency, "per": per}


def test_profile_refuses_two_pages_disagreeing_without_a_conflict() -> None:
    two = [_price("229", "p/1"), _price("199", "p/2")]
    view = _View(_slots(_profile(prices=two)), key="k")
    assert "conflict" in checks.profile_done(view).message
    kept = _profile(prices=two, conflicts=[{"field": "price", "per": "unit"}])
    assert checks.profile_done(_View(_slots(kept), key="k")).ok


def test_profile_reads_one_amount_in_two_currencies_as_a_conflict() -> None:
    two = [_price("229", "p/1", "USD"), _price("229", "p/2", "EUR")]
    view = _View(_slots(_profile(prices=two)), key="k")
    assert "conflict" in checks.profile_done(view).message
    same = [_price("229", "p/1"), _price("229", "p/2")]
    assert checks.profile_done(_View(_slots(_profile(prices=same)), key="k")).ok


def test_profile_reads_tiers_on_one_page_as_no_conflict() -> None:
    tiers = [_price("229", "p/1"), _price("249", "p/1")]
    assert checks.profile_done(_View(_slots(_profile(prices=tiers)), key="k")).ok


def test_profile_refuses_a_scope_dimension_left_empty_and_no_price() -> None:
    view = _View(_slots(_profile(cells={"power": []})), key="k")
    assert "power" in checks.profile_done(view).message
    view = _View(_slots(_profile(cells={})), key="k")
    assert not checks.profile_done(view).ok
    view = _View(_slots(_profile(prices=[])), key="k")
    assert "prices" in checks.profile_done(view).message


def test_needs_must_say_n_of_m_in_this_sample() -> None:
    need = {"theme": "battery", "phrase": "7 of 40 reviews in this sample", "evidence": [{}] * 7}
    good = {"sample": {"rows": 42, "unique": 40, "duplicates": [{}, {}]}, "needs": [need]}
    Slots = dict[tuple[str, str | None], Mapping[str, Any]]

    def view(needs: Mapping[str, Any]) -> _View:
        slots: Slots = {("needs", None): needs, ("scope", None): _scope("EU")}
        return _View(slots)

    assert checks.needs_done(view(good)).ok
    share = {**need, "phrase": "17.5% of customers"}
    assert not checks.needs_done(view({**good, "needs": [share]})).ok
    miscount = {**need, "evidence": [{}] * 6}
    assert not checks.needs_done(view({**good, "needs": [miscount]})).ok
    # A scope theme with no need saved is refused by name.
    assert "battery" in checks.needs_done(view({**good, "needs": []})).message


def test_report_needs_every_row_and_proposal_thresholds() -> None:
    slots: dict[tuple[str, str | None], Mapping[str, Any]] = {
        ("competitors", None): {"companies": [{"key": "a"}, {"key": "b"}]},
        ("matrix", None): {"rows": [{"key": "a"}]},
        ("findings", None): {"findings": [], "unknowns": []},
        ("tests", None): {"tests": [{"id": "T1", "threshold": {"status": "proposal"}}]},
        ("scope", None): _scope("EU"),
    }
    assert not checks.report_done(_View(slots)).ok
    full = {"prices": [{"value": "x"}], "cells": {"power": [{"value": "x"}]}}
    slots[("matrix", None)] = {"rows": [{"key": "a", **full}, {"key": "b", **full}]}
    assert checks.report_done(_View(slots)).ok
    empty = {**full, "cells": {"power": []}}
    slots[("matrix", None)] = {"rows": [{"key": "a", **full}, {"key": "b", **empty}]}
    assert "b/power" in checks.report_done(_View(slots)).message
    slots[("matrix", None)] = {"rows": [{"key": "a", **full}, {"key": "b", **full}]}
    slots[("tests", None)] = {"tests": [{"id": "T1", "threshold": {"status": "decided"}}]}
    assert not checks.report_done(_View(slots)).ok


def test_the_run_completes_only_with_its_four_outputs() -> None:
    slots: dict[tuple[str, str | None], Mapping[str, Any]] = {(k, None): {} for k in checks.OUTPUTS}
    assert checks.outputs_exist(_View(slots)).ok
    del slots[("tests", None)]
    assert checks.outputs_exist(_View(slots)).keys == ("tests",)


def test_each_failing_check_states_what_it_found_what_it_expected_and_the_change() -> None:
    """A real model fixes its work in one turn when the message names the values and the fix."""
    slots: dict[tuple[str, str | None], Mapping[str, Any]] = {
        ("competitors", None): {"companies": [_company("a", "p/1"), _company("b", "p/2")],
                                "resolved": 3, "pages_found": 1},
    }  # fmt: skip
    message = checks.discover_done(_View(slots)).message
    assert "resolved is 3 and pages_found 1" in message
    assert "set resolved to 2 and pages_found to 2" in message
    twice = {"companies": [_company("a", "p/1"), _company("b", "p/1")]}
    assert (
        "{'p/1': ['a', 'b']}" in checks.discover_done(_View({("competitors", None): twice})).message
    )

    themes = [*THEMES, {"id": "setup", "name": "setup"}, {"id": "price", "name": "price"}]
    need = {"theme": "battery", "phrase": "2 of 9 reviews in this sample", "evidence": [{}] * 2}
    needs = {"sample": {"rows": 9, "unique": 9, "duplicates": []}, "needs": [need]}
    view = _View({("needs", None): needs, ("scope", None): _scope("EU", themes=themes)})
    result = checks.needs_done(view)
    assert result.message.startswith(
        "needs saved for themes ['battery']; missing a need for themes ['price', 'setup']: "
        "save the needs again with one need per theme of the scope"
    )
    assert result.keys == ("price", "setup")
    rows = {"sample": {"rows": 12, "unique": 9, "duplicates": [{}]}, "needs": [need]}
    view = _View({("needs", None): rows, ("scope", None): _scope("EU")})
    assert "sample.rows is 12, but 9 unique reviews and 1 duplicates make 10: set rows to 10" in (
        checks.needs_done(view).message
    )
    off = {**need, "phrase": "2 of 8 reviews in this sample"}
    view = _View({("needs", None): {**needs, "needs": [off]}, ("scope", None): _scope("EU")})
    assert "expected '2 of 9 reviews in this sample'" in checks.needs_done(view).message
    cited = {**need, "evidence": [{"review": "r1"}, {"review": "r2"}],
             "phrase": "2 of 1 reviews in this sample"}  # fmt: skip
    few = {"sample": {"rows": 1, "unique": 1, "duplicates": []}, "needs": [cited]}
    view = _View({("needs", None): few, ("scope", None): _scope("EU")})
    assert "cite 2 distinct reviews" in checks.needs_done(view).message

    report: dict[tuple[str, str | None], Mapping[str, Any]] = {
        ("competitors", None): {"companies": [{"key": "a"}, {"key": "b"}]},
        ("matrix", None): {"rows": [{"key": "a"}, {"key": "c"}]},
        ("scope", None): _scope("EU"),
    }
    assert "add rows for ['b'], remove rows for ['c']" in checks.report_done(_View(report)).message


@dataclass
class _Read:
    """A ``read.recorded`` fact, as the log holds one."""

    payload: Mapping[str, Any]
    type: str = "read.recorded"


def _fetched(*records: str) -> list[Any]:
    return [_Read({"input": {"kind": "record", "id": r, "version": "sha256:1"}}) for r in records]


ABSENCE = {"absence": {"statement": "no page read sells the product",
                       "scope": {"records": ["record:web/a.example/p@sha256:1"]}}}  # fmt: skip
CITE = {"span": "EX-1", "quote": "a copy of the original page"}


def _empty(**extra: Any) -> dict[str, Any]:
    return {"companies": [], "resolved": 0, "pages_found": 0, **extra}


def test_an_empty_comparison_without_a_scoped_absence_claim_fails_and_says_what_to_do() -> None:
    """The live run that left every seller out on a page's synthetic label passed this check."""
    excluded = [{"record": "web/a.example/p", "reason": "the page says it is synthetic",
                 "quote": "Invented for testing"}]  # fmt: skip
    view = _View({("scope", None): _scope("EU"),
                  ("competitors", None): _empty(excluded_fixtures=excluded)})  # fmt: skip
    view.log.events.extend(_fetched("web/a.example/p"))
    result = checks.discover_done(view)
    assert not result.ok
    assert "list each company whose own page sells" in result.message
    assert '{"absence": {"statement"' in result.message and "excluded" in result.message


def _minted(**spans: str) -> _Event:
    """An ``excerpts.minted`` fact minting each span ID for its record."""
    rows = [{"span_id": s, "record_id": r} for s, r in spans.items()]
    return _Event("excerpts.minted", {"tool": "fetch_url", "spans": rows, "shown": []})


def _cite(span: str, quote: str) -> dict[str, str]:
    return {"span": span, "quote": quote}


def test_an_empty_comparison_passes_as_a_scoped_absence_with_every_fetched_page_excluded() -> None:
    view = _View({("scope", None): _scope("EU")})
    view.log.events.extend(_fetched("web/a.example/p", "web/b.example/q"))
    view.log.events.append(_minted(**{"EX-1": "web/a.example/p", "EX-2": "web/b.example/q"}))
    one = [{"record": "web/a.example/p", "reason": "copy", "cite": CITE}]
    view.slots[("competitors", None)] = _empty(absence=ABSENCE, excluded=one)
    left = checks.discover_done(view)
    assert not left.ok and "web/b.example/q" in left.message  # a fetched page unaccounted for
    advert = {"record": "record:web/b.example/q@sha256:1", "reason": "advert",
              "cite": _cite("EX-2", "Deals of the day on every brand")}  # fmt: skip
    view.slots[("competitors", None)] = _empty(absence=ABSENCE, excluded=[*one, advert])
    assert checks.discover_done(view).ok


LABEL = "Invented for testing; not a real company or offer."


def test_the_live_runs_retry_on_the_synthetic_label_fails() -> None:
    """The live run's own cite, reworded as an allowed reason, still leaves out every page.

    Four seller pages, each excluded as ``not_a_seller`` on the same label line: the reason
    is allowed and each cite is that page's own span, but one quote leaving out four pages
    is a label every page carries, not a reason about one.
    """
    pages = [f"web/{n}.example/product" for n in ("kelvra", "tallyway", "noctra", "brightfield")]
    view = _View({("scope", None): _scope("EU")})
    view.log.events.extend(_fetched(*pages))
    view.log.events.append(_minted(**{f"EX-{i}": p for i, p in enumerate(pages, start=1)}))
    retry = [{"record": p, "reason": "not_a_seller", "cite": _cite(f"EX-{i}", LABEL)}
             for i, p in enumerate(pages, start=1)]  # fmt: skip
    scoped = {"records": [f"record:{p}@sha256:1" for p in pages]}
    absence = {"absence": {"statement": "no page fetched sells an occupancy sensor",
                           "scope": scoped}}  # fmt: skip
    view.slots[("competitors", None)] = _empty(absence=absence, excluded=retry)
    result = checks.discover_done(view)
    assert not result.ok and "the same words" in result.message
    assert all(p in result.message for p in pages)


def test_an_exclusion_cites_the_record_it_leaves_out() -> None:
    view = _View({("scope", None): _scope("EU")})
    view.log.events.extend(_fetched("web/a.example/p"))
    view.log.events.append(_minted(**{"EX-1": "web/a.example/p", "EX-9": "input/notes.md"}))
    elsewhere = [{"record": "web/a.example/p", "reason": "advert",
                  "cite": _cite("EX-9", "a note about adverts")}]  # fmt: skip
    view.slots[("competitors", None)] = _empty(absence=ABSENCE, excluded=elsewhere)
    result = checks.discover_done(view)
    assert not result.ok and "not spans of those records" in result.message
    unminted = [{"record": "web/a.example/p", "reason": "advert", "cite": _cite("EX-7", "x")}]
    view.slots[("competitors", None)] = _empty(absence=ABSENCE, excluded=unminted)
    assert not checks.discover_done(view).ok


def test_an_empty_market_in_the_scope_may_leave_out_a_seller_outside_it() -> None:
    """A page that sells the product only outside the scope's geography is out_of_scope."""
    view = _View({("scope", None): _scope("EU")})
    view.log.events.extend(_fetched("web/us.example/p"))
    view.log.events.append(_minted(**{"EX-1": "web/us.example/p"}))
    us = [{"record": "web/us.example/p", "reason": "out_of_scope",
           "cite": _cite("EX-1", "Shipping to the United States only")}]  # fmt: skip
    view.slots[("competitors", None)] = _empty(absence=ABSENCE, excluded=us)
    assert checks.discover_done(view).ok


def test_an_empty_comparison_accounts_for_every_input_read_too() -> None:
    """An input read in discover is a record to account for, as a fetched page is."""
    view = _View({("scope", None): _scope("EU")})
    view.log.events.extend(_fetched("definitions/research/doc_kind", "input/reviews.json",
                                    "input/sites/a.html"))  # fmt: skip
    view.log.events.append(_minted(**{"EX-1": "input/reviews.json"}))
    view.slots[("competitors", None)] = _empty(absence=ABSENCE, excluded=[])
    result = checks.discover_done(view)
    assert not result.ok and "input/sites/a.html" in result.message
    assert "definitions/" not in result.message  # the runtime's own read is not judged


def test_an_empty_web_market_scopes_its_absence_on_every_web_search() -> None:
    query = 'research.web_search?{"query":"occupancy sensor"}'
    view = _View({("scope", None): _scope("EU")})
    view.log.events.append(_Event("read.recorded", {"input": {"kind": "query", "id": query,
                                                              "version": "q-1"}}))  # fmt: skip
    on_records = {"absence": {"statement": "none sells it", "scope": {"records": ["record:x@1"]}}}
    view.slots[("competitors", None)] = _empty(absence=on_records, excluded=[])
    result = checks.discover_done(view)
    assert not result.ok and query in result.message
    for scoped in (f"query:{query}@q-1", [f"query:{query}@q-1"], [query]):
        claim = {"absence": {"statement": "none sells it", "scope": {"query": scoped}}}
        view.slots[("competitors", None)] = _empty(absence=claim, excluded=[])
        assert checks.discover_done(view).ok, scoped


def test_a_synthesis_lists_no_companies_and_needs_no_absence_claim() -> None:
    scope = _scope("", plan={"kind": "synthesis"}, segment={})
    assert checks.discover_done(_View({("scope", None): scope, ("competitors", None): _empty()})).ok


def test_an_absence_claim_needs_its_statement_and_scope() -> None:
    view = _View({("scope", None): _scope("EU")})
    for bad in ({"absence": {"statement": "none"}}, {"absence": {"scope": {"query": "q"}}},
                {"statement": "none", "scope": {"query": "q"}}):  # fmt: skip
        view.slots[("competitors", None)] = _empty(absence=bad)
        assert not checks.discover_done(view).ok, bad
