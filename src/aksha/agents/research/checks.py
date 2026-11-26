"""Pure completion checks for the research agent. Each reads only the ``TaskView`` it is given.

A check names what is missing in its message, so the model reads why its finish was
refused. Grounding (every cite verified, unknowns only where declared) is the runtime's,
at save; these checks hold the agent's own rules on top. What to compare is the scope's,
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

from . import analysis

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


def scope_done(view: Any) -> CheckResult:
    """The scope names a segment, a geography, a boundary and what to compare, with sources."""
    scope = _content(view, "scope")
    if scope is None:
        return CheckResult(False, "save the scope")
    missing = [f for f in ("segment", "geography") if not scope.get(f, {}).get("value")]
    if missing:
        return CheckResult(False, f"scope has no value for {missing}", keys=tuple(missing))
    unsourced = [f for f in ("segment", "geography") if not scope[f].get("basis")]
    if unsourced:
        return CheckResult(False, f"say where {unsourced} came from", keys=tuple(unsourced))
    boundary = scope.get("boundary")
    if not isinstance(boundary, Mapping) or not boundary.get("in") or "out" not in boundary:
        return CheckResult(False, "the scope needs a boundary: what is in and what is out")
    dims = scope.get("dimensions")
    if not isinstance(dims, list | tuple):
        return CheckResult(False, "list the dimensions to compare (an empty list compares price)")
    bad = [d for d in dims if not isinstance(d, Mapping) or not d.get("id") or not d.get("terms")]
    ids = dimension_ids(scope)
    if bad or len(ids) != len(set(ids)):
        return CheckResult(False, "each dimension has a unique id and the terms that find it")
    themes = scope.get("themes")
    if not isinstance(themes, list | tuple) or any(
        not isinstance(t, Mapping) or not t.get("id") or not t.get("terms") for t in themes
    ):
        return CheckResult(False, "list the review themes, each with an id and its terms")
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
    pages = [p["record"] for c in companies for p in c.get("pages", [])]
    if len(pages) != len(set(pages)):
        return CheckResult(False, "a page is listed under two companies")
    if found.get("resolved") != len(companies) or found.get("pages_found") != len(pages):
        return CheckResult(False, "resolved and pages_found must count the companies and pages")
    merged = [c["key"] for c in companies if len(c.get("pages", [])) > 1]
    linked = _relations(view, "same_entity_as")
    unlinked = [
        c["key"]
        for c in companies
        if c["key"] in merged and not _joined([p["record"] for p in c["pages"]], linked)
    ]
    if unlinked:
        return CheckResult(
            False, f"merge {unlinked} with same_entity_as and its excerpt", keys=tuple(unlinked)
        )
    unproven = [c["key"] for c in companies if c["key"] in merged and not c.get("merge_evidence")]
    if unproven:
        return CheckResult(False, f"cite the merge evidence of {unproven}", keys=tuple(unproven))
    return CheckResult(True)


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
        return CheckResult(False, f"cells {empty} need a cited entry or an explicit unknown")
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
        return CheckResult(False, f"prices per {open_} differ across pages: record the conflict")
    return CheckResult(True)


def needs_done(view: Any) -> CheckResult:
    """Every scope theme is counted as "N of M reviews in this sample", M the unique rows."""
    found = _content(view, "needs")
    if found is None:
        return CheckResult(False, "save the needs")
    sample = found.get("sample", {})
    unique, rows = sample.get("unique"), sample.get("rows")
    if not isinstance(unique, int) or rows != unique + len(sample.get("duplicates", [])):
        return CheckResult(False, "rows must equal unique reviews plus duplicates")
    scope = _content(view, "scope") or {}
    want = {str(t.get("id")) for t in scope.get("themes", ()) if isinstance(t, Mapping)}
    have = {str(n.get("theme")) for n in found.get("needs", [])}
    if want - have:
        return CheckResult(False, f"themes {sorted(want - have)} have no need saved")
    bad = []
    for need in found.get("needs", []):
        m = PHRASE.match(str(need.get("phrase", "")))
        n = len(need.get("evidence", []))
        if m is None or int(m.group(1)) != n or int(m.group(2)) != unique:
            bad.append(str(need.get("theme")))
    if bad:
        return CheckResult(
            False, f"needs {bad}: say 'N of M reviews in this sample'", keys=tuple(bad)
        )
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
        return CheckResult(False, f"matrix rows {have} are not the companies {want}")
    dims = dimension_ids(_content(view, "scope"))
    empty = [f"{r.get('key')}/{c}" for r in matrix.get("rows", []) for c in _missing_cells(r, dims)]
    if empty:
        return CheckResult(False, f"cells {empty} need a cited entry or an explicit unknown")
    findings = _content(view, "findings")
    if findings is None:
        return CheckResult(False, "save the findings")
    tests = _content(view, "tests")
    if tests is None or "tests" not in tests:
        return CheckResult(False, "save what to test next")
    unmarked = [
        t.get("id") for t in tests["tests"] if t.get("threshold", {}).get("status") != "proposal"
    ]
    if unmarked:
        return CheckResult(False, f"mark the thresholds of {unmarked} as a proposal")
    return CheckResult(True)


def outputs_exist(view: Any) -> CheckResult:
    """The run's four outputs exist at current versions."""
    missing = [k for k in OUTPUTS if view.latest(k) is None]
    return CheckResult(not missing, f"missing outputs: {missing}", keys=tuple(missing))
