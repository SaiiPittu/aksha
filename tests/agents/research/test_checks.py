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
        **extra,
    }


def test_scope_needs_segment_and_geography() -> None:
    view = _View({("scope", None): _scope("")})
    assert not checks.scope_done(view).ok
    view.slots[("scope", None)] = _scope("EU")
    assert checks.scope_done(view).ok


def test_scope_needs_where_each_value_came_from_and_a_boundary() -> None:
    assert not checks.scope_done(_View({("scope", None): _scope("EU", basis="")})).ok
    assert not checks.scope_done(_View({("scope", None): _scope("EU", boundary={})})).ok


def test_scope_needs_what_to_compare_each_with_its_terms() -> None:
    assert checks.scope_done(_View({("scope", None): _scope("EU", dimensions=[], themes=[])})).ok
    no_terms = [{"id": "power", "name": "power", "terms": []}]
    assert not checks.scope_done(_View({("scope", None): _scope("EU", dimensions=no_terms)})).ok
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
