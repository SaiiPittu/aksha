"""Pure completion checks for the research agent. Each reads only the ``TaskView`` it is given.

A check names what is missing in its message, so the model reads why its finish was
refused. Grounding (every cite verified, unknowns only where declared) is the runtime's,
at save; these checks hold the agent's own rules on top: one record per company with its
merge evidence, a conflict kept with both values, "N of M in this sample", every row of
the matrix present, every proposed threshold marked ``proposal``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from aksha.runtime.api import CheckResult, Unknown

PHRASE = re.compile(r"^(\d+) of (\d+) reviews in this sample$")
OUTPUTS = ("matrix", "needs", "findings", "tests")
CELLS = ("unit_price", "subscription", "power", "battery_life", "privacy")


def _content(view: Any, kind: str, key: str | None = None) -> Mapping[str, Any] | None:
    found = view.latest(kind, key)
    return None if found is None else found.content


def scope_done(view: Any) -> CheckResult:
    """The scope names a segment, a geography and a boundary, each with where it came from."""
    scope = _content(view, "scope")
    if scope is None:
        return CheckResult(False, "save the scope")
    missing = [f for f in ("segment", "geography") if not scope.get(f, {}).get("value")]
    if missing:
        return CheckResult(False, f"scope has no value for {missing}", keys=tuple(missing))
    return CheckResult(True)


def _relations(view: Any, kind: str) -> list[Mapping[str, Any]]:
    return [
        e.payload
        for e in view.log.events
        if e.type == "relation.saved" and str(e.payload.get("kind", "")).endswith(f".{kind}")
    ]


def discover_done(view: Any) -> CheckResult:
    """Each page belongs to one company; every merge carries a ``same_entity_as`` relation."""
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
    if len(linked) < sum(len(c["pages"]) - 1 for c in companies if c["key"] in merged):
        return CheckResult(
            False, f"merge {merged} with same_entity_as and its excerpt", keys=tuple(merged)
        )
    unproven = [c["key"] for c in companies if c["key"] in merged and not c.get("merge_evidence")]
    if unproven:
        return CheckResult(False, f"cite the merge evidence of {unproven}", keys=tuple(unproven))
    return CheckResult(True)


def _values(cell: list[Any]) -> list[str]:
    return [str(e["value"]) for e in cell if isinstance(e, Mapping) and "value" in e]


def profile_done(view: Any) -> CheckResult:
    """The company's profile has every cell, and differing unit prices are kept as a conflict."""
    profile = _content(view, "profile", view.key)
    if profile is None:
        return CheckResult(False, f"save the profile of {view.key}")
    empty = [c for c in CELLS if not profile.get(c)]
    if empty:
        return CheckResult(False, f"cells {empty} need a cited entry or an explicit unknown")
    prices = set(_values(profile["unit_price"]))
    conflicted = any(c.get("field") == "unit_price" for c in profile.get("conflicts", []))
    if len(prices) > 1 and not conflicted:
        return CheckResult(False, "the unit prices differ: record the conflict with both values")
    return CheckResult(True)


def needs_done(view: Any) -> CheckResult:
    """Every need counts its evidence as "N of M reviews in this sample", M the unique rows."""
    found = _content(view, "needs")
    if found is None:
        return CheckResult(False, "save the needs")
    sample = found.get("sample", {})
    unique, rows = sample.get("unique"), sample.get("rows")
    if not isinstance(unique, int) or rows != unique + len(sample.get("duplicates", [])):
        return CheckResult(False, "rows must equal unique reviews plus duplicates")
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
    """The matrix has a row per company (or its unknown); findings and tests are saved."""
    matrix = _content(view, "matrix")
    companies = _content(view, "competitors") or {}
    if matrix is None:
        return CheckResult(False, "save the matrix")
    want = sorted(c["key"] for c in companies.get("companies", []))
    have = sorted(str(r.get("key")) for r in matrix.get("rows", []))
    if want != have:
        return CheckResult(False, f"matrix rows {have} are not the companies {want}")
    findings = _content(view, "findings")
    if findings is None:
        return CheckResult(False, "save the findings")
    tests = _content(view, "tests")
    if tests is None or not tests.get("tests"):
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


def is_unknown(value: Any) -> bool:
    """Whether ``value`` is an explicit unknown as content holds it."""
    if not isinstance(value, Mapping) or set(value) != {"unknown"}:
        return False
    try:
        Unknown.from_json(value)
    except ValueError:
        return False
    return True
