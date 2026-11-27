"""Pure completion checks for the research agent. Each reads only the ``TaskView`` it is given.

A check's message states what it found, what it expected and the exact change that
passes, so a model can fix its work in one turn. The counts a check compares (companies
resolved, pages found, review rows, each theme's "N of M") are computed at save
(:mod:`.computed`), never asked of the model; a check still holds them, against a
correction or a malformed save, and says the computed value.

Grounding (every cite verified, unknowns only where declared) is the runtime's, at save;
these checks hold the agent's own rules on top. What to compare is the scope's,
taken from the request: the scope lists its ``dimensions`` (the matrix columns besides
price) and its ``themes`` (what to read the reviews for), and every later check holds the
work to that list, so no column or theme is fixed in code.

The competitors carry the scope's dimensions on, so each company's evidence reads only the
competitors: a scope change that leaves the dimensions as they were rebuilds the
competitors equal and stops there (early cutoff, ADR-P05).

The rules: one record per company with its merge evidence; a price conflict kept with
both values; "N of M reviews in this sample"; every company in the matrix with every
dimension cited or an explicit unknown; every proposed threshold marked ``proposal``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from aksha.runtime.api import CheckResult

from . import analysis, computed

PHRASE = re.compile(r"^(\d+) of (\d+) reviews in this sample$")
OUTPUTS = ("matrix", "needs", "findings", "tests")


def _content(view: Any, kind: str, key: str | None = None) -> Mapping[str, Any] | None:
    found = view.latest(kind, key)
    return None if found is None else found.content


def dimension_ids(scope: Mapping[str, Any] | None) -> list[str]:
    """The comparison dimensions of a scope (or of the competitors, which carry them on)."""
    if not scope:
        return []
    return [str(d.get("id")) for d in scope.get("dimensions", ()) if isinstance(d, Mapping)]


PLAN_KINDS = ("comparison", "synthesis")
"""A market comparison (sellers, prices, a matrix) or a synthesis of what the inputs say."""


def plan_kind(content: Mapping[str, Any] | None) -> str:
    """The plan a scope (or the competitors, which carry it on) names; ``comparison`` if none."""
    plan = (content or {}).get("plan")
    return str(plan.get("kind")) if isinstance(plan, Mapping) else "comparison"


def scope_done(view: Any) -> CheckResult:
    """The scope names its plan, a boundary and what to compare or read for, with sources.

    A comparison also names its segment and geography; a synthesis needs neither.
    """
    scope = _content(view, "scope")
    if scope is None:
        return CheckResult(False, "save the scope")
    plan = scope.get("plan")
    if not isinstance(plan, Mapping) or plan.get("kind") not in PLAN_KINDS:
        got = plan.get("kind") if isinstance(plan, Mapping) else plan
        return CheckResult(
            False, f"the scope's plan kind is {got!r}: set plan.kind to one of {list(PLAN_KINDS)}"
        )
    comparison = plan.get("kind") == "comparison"
    needed = ("segment", "geography") if comparison else ()
    missing = [f for f in needed if not scope.get(f, {}).get("value")]
    if missing:
        return CheckResult(
            False, f"scope has no value for {missing}: give each a value and its basis",
            keys=tuple(missing),
        )  # fmt: skip
    unsourced = [f for f in needed if not scope[f].get("basis")]
    if unsourced:
        return CheckResult(
            False, f"{unsourced} have no basis: say where each came from (request, answer, "
            "an input)", keys=tuple(unsourced),
        )  # fmt: skip
    boundary = scope.get("boundary")
    if not isinstance(boundary, Mapping) or not boundary.get("in") or "out" not in boundary:
        return CheckResult(False, "the scope needs a boundary: what is in and what is out")
    dims = scope.get("dimensions")
    if not isinstance(dims, list | tuple):
        return CheckResult(False, "list the dimensions to compare (an empty list compares price)")
    bad = [d for d in dims if not isinstance(d, Mapping) or not d.get("id")]
    ids = dimension_ids(scope)
    if bad or len(ids) != len(set(ids)):
        twice = sorted({i for i in ids if ids.count(i) > 1})
        return CheckResult(
            False, f"{len(bad)} dimensions have no id and ids {twice} repeat: give each "
            "dimension its own id",
        )  # fmt: skip
    themes = scope.get("themes")
    if not isinstance(themes, list | tuple) or any(
        not isinstance(t, Mapping) or not t.get("id") for t in themes
    ):
        return CheckResult(
            False, "list the themes to read the customer voices for, each with an id"
        )
    if not comparison and not themes:
        return CheckResult(False, "a synthesis names the themes it reads the inputs for")
    return CheckResult(True)


def _relations(view: Any, kind: str) -> list[Mapping[str, Any]]:
    return [
        e.payload
        for e in view.log.events
        if e.type == "relation.saved" and str(e.payload.get("kind", "")).endswith(f".{kind}")
    ]


def _joined(pages: list[str], links: list[Mapping[str, Any]]) -> bool:
    """Whether ``links`` (relations with ``src`` and ``dst`` records) connect all ``pages``."""
    group = {pages[0]}
    grew = True
    while grew:
        grew = False
        for link in links:
            ends = {str(link.get("src", {}).get("id")), str(link.get("dst", {}).get("id"))}
            if len(ends & group) == 1 and ends <= set(pages):
                group |= ends
                grew = True
    return group == set(pages)


def discover_done(view: Any) -> CheckResult:
    """Each page belongs to one company; a merged company's pages are joined by ``same_entity_as``.

    The relations must join exactly that company's pages: a relation between other pages,
    or one fewer than the merge needs, leaves the check failing.
    """
    found = _content(view, "competitors")
    if found is None:
        return CheckResult(False, "save the competitors")
    companies = found.get("companies", [])
    # A page is a record, or one section of a record that holds several sellers' sections.
    pages = [(p["record"], p.get("section")) for c in companies for p in c.get("pages", [])]
    if len(pages) != len(set(pages)):
        owners: dict[tuple[str, Any], list[str]] = {}
        for c in companies:
            for p in c.get("pages", []):
                owners.setdefault((p["record"], p.get("section")), []).append(str(c.get("key")))
        shared = {_page_name(p): ks for p, ks in owners.items() if len(ks) > 1}
        return CheckResult(
            False, f"pages listed under more than one company: {shared}; a page belongs to "
            "one company: keep each under one and merge the companies if they are one",
        )  # fmt: skip
    counted = (found.get("resolved"), found.get("pages_found"))
    if counted != (len(companies), len(pages)):
        return CheckResult(
            False, f"resolved is {counted[0]!r} and pages_found {counted[1]!r}, but the "
            f"competitors list {len(companies)} companies on {len(pages)} pages: set resolved "
            f"to {len(companies)} and pages_found to {len(pages)}",
        )  # fmt: skip
    merged = [c["key"] for c in companies if len(c.get("pages", [])) > 1]
    linked = _relations(view, "same_entity_as")
    unlinked = [
        c["key"]
        for c in companies
        if c["key"] in merged and not _joined([p["record"] for p in c["pages"]], linked)
    ]
    if unlinked:
        join = {k: [p["record"] for c in companies if c["key"] == k for p in c["pages"]]
                for k in unlinked}  # fmt: skip
        return CheckResult(
            False, f"companies {unlinked} list several pages that no same_entity_as relation "
            f"joins: save the competitors again with same_entity_as relations joining each "
            f"one's pages {join}, each with the excerpt that says they are one company",
            keys=tuple(unlinked),
        )  # fmt: skip
    unproven = [c["key"] for c in companies if c["key"] in merged and not c.get("merge_evidence")]
    if unproven:
        return CheckResult(
            False, f"companies {unproven} merge several pages but their merge_evidence is "
            "empty: cite in each the excerpt that says the pages are one company",
            keys=tuple(unproven),
        )  # fmt: skip
    return CheckResult(True)


def _page_name(page: tuple[str, Any]) -> str:
    return page[0] if page[1] is None else f"{page[0]} section {page[1]}"


def _missing_cells(entry: Mapping[str, Any], dims: list[str]) -> list[str]:
    cells = entry.get("cells", {})
    out = [] if entry.get("prices") else ["prices"]
    return out + [d for d in dims if not isinstance(cells, Mapping) or not cells.get(d)]


def profile_done(view: Any) -> CheckResult:
    """The company's profile has prices and every scope dimension; a conflict is kept open."""
    profile = _content(view, "profile", view.key)
    if profile is None:
        return CheckResult(False, f"save the profile of {view.key}")
    empty = _missing_cells(profile, dimension_ids(_content(view, "competitors")))
    if empty:
        return CheckResult(
            False, f"the profile of {view.key} has no entry in {empty}: add to each a cited "
            "entry, or an explicit unknown with its question and reason",
        )  # fmt: skip
    points = [
        {"record": e.get("record"), "per": e.get("per"), "currency": e.get("currency"),
         "value": e["value"]}
        for e in profile["prices"]
        if isinstance(e, Mapping) and "value" in e
    ]  # fmt: skip
    recorded = {
        str(c.get("per")) for c in profile.get("conflicts", []) if c.get("field") == "price"
    }
    open_ = [b for b in analysis.price_conflict(points) if b not in recorded]
    if open_:
        entries = [{"field": "price", "per": b} for b in open_]
        return CheckResult(
            False, f"prices per {open_} differ across pages and conflicts records none of "
            f"them: add {entries} to conflicts, keeping every price as listed",
        )  # fmt: skip
    return CheckResult(True)


def needs_done(view: Any) -> CheckResult:
    """Every scope theme is counted as "N of M reviews in this sample", M the unique rows."""
    found = _content(view, "needs")
    if found is None:
        return CheckResult(False, "save the needs")
    sample = found.get("sample", {})
    unique, rows = sample.get("unique"), sample.get("rows")
    copies = len(sample.get("duplicates", []))
    if not isinstance(unique, int) or isinstance(unique, bool) or unique < 0:
        return CheckResult(
            False, f"sample.unique is {unique!r}: set it to the number of distinct reviews "
            "read, cross-posted copies counted once (rows and each need's phrase are "
            "computed from it at save)",
        )  # fmt: skip
    if rows != unique + copies:
        return CheckResult(
            False, f"sample.rows is {rows!r}, but {unique} unique reviews and {copies} "
            f"duplicates make {unique + copies}: set rows to {unique + copies}",
        )  # fmt: skip
    distinct = {
        str(e["review"])
        for n in found.get("needs", [])
        for e in computed.cited_reviews(n) or []
        if isinstance(e, Mapping) and e.get("review") is not None
    }
    if len(distinct) > unique:
        return CheckResult(
            False, f"sample.unique is {unique}, but the needs cite {len(distinct)} distinct "
            f"reviews: set unique to the number of distinct reviews read, at least "
            f"{len(distinct)}",
        )  # fmt: skip
    scope = _content(view, "scope") or {}
    want = {str(t.get("id")) for t in scope.get("themes", ()) if isinstance(t, Mapping)}
    if (
        want
        and unique == 0
        and (sample.get("status") != "no_feedback_found" or not sample.get("checked"))
    ):
        return CheckResult(False, "no customer voice was read: read them, or say "
                           "status no_feedback_found with the listings checked")  # fmt: skip
    have = {str(n.get("theme")) for n in found.get("needs", [])}
    if want - have:
        missing = sorted(want - have)
        return CheckResult(
            False, f"needs saved for themes {sorted(have & want)}; missing a need for themes "
            f"{missing}: save the needs again with one need per theme of the scope "
            f"({sorted(want)}), keeping the needs already saved",
            keys=tuple(missing),
        )  # fmt: skip
    bad = {}
    for need in found.get("needs", []):
        got = str(need.get("phrase", ""))
        m = PHRASE.match(got)
        n = len(computed.cited_reviews(need) or [])
        if m is None or int(m.group(1)) != n or int(m.group(2)) != unique:
            bad[str(need.get("theme"))] = (got, computed.phrase(n, unique))
    if bad:
        said = "; ".join(f"{t}: {g!r}, expected {w!r}" for t, (g, w) in bad.items())
        return CheckResult(
            False, f"needs {sorted(bad)} do not say 'N of M reviews in this sample' with N "
            f"the reviews each cites and M the {unique} unique reviews ({said}): set each "
            "phrase to the expected one",
            keys=tuple(bad),
        )  # fmt: skip
    return CheckResult(True)


def report_done(view: Any) -> CheckResult:
    """The matrix has a row per company with every dimension; findings and tests are saved."""
    matrix = _content(view, "matrix")
    companies = _content(view, "competitors") or {}
    if matrix is None:
        return CheckResult(False, "save the matrix")
    want = sorted(c["key"] for c in companies.get("companies", []))
    have = sorted(str(r.get("key")) for r in matrix.get("rows", []))
    if want != have:
        add, drop = sorted(set(want) - set(have)), sorted(set(have) - set(want))
        twice = sorted({k for k in have if have.count(k) > 1})
        return CheckResult(
            False, f"the matrix has rows {have}, but the companies are {want}: add rows for "
            f"{add}, remove rows for {drop} and keep one row per company (repeated: {twice})",
        )  # fmt: skip
    dims = dimension_ids(_content(view, "scope"))
    empty = [f"{r.get('key')}/{c}" for r in matrix.get("rows", []) for c in _missing_cells(r, dims)]
    if empty:
        return CheckResult(
            False, f"matrix cells {empty} are empty: add to each a cited entry, or an "
            "explicit unknown with its question and reason",
        )  # fmt: skip
    findings = _content(view, "findings")
    if findings is None:
        return CheckResult(False, "save the findings")
    tests = _content(view, "tests")
    if plan_kind(_content(view, "scope")) == "synthesis" and tests is None:
        return CheckResult(True)
    if tests is None or "tests" not in tests:
        return CheckResult(False, "save what to test next")
    unmarked = [
        t.get("id") for t in tests["tests"] if t.get("threshold", {}).get("status") != "proposal"
    ]
    if unmarked:
        return CheckResult(
            False, f"tests {unmarked} have a threshold whose status is not 'proposal': set "
            "threshold.status to 'proposal' in each",
        )  # fmt: skip
    return CheckResult(True)


def outputs_exist(view: Any) -> CheckResult:
    """The run's outputs exist at current versions: a synthesis needs no tests."""
    want = OUTPUTS if plan_kind(_content(view, "scope")) == "comparison" else OUTPUTS[:3]
    missing = [k for k in want if view.latest(k) is None]
    return CheckResult(
        not missing, f"outputs {missing} are not saved: save each ({list(want)} make the run)",
        keys=tuple(missing),
    )  # fmt: skip
