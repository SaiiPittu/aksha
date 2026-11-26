"""The report: built from the results projection, in the order the eval design sets.

``report.txt`` starts with the self-test line and its mutation coverage, then the mode
label, because a failed self-test makes everything after it untrustworthy. Then the
headline (unique case and trap items passed, with the world cluster count), invariant
failures, failure clusters, the case table, relations and the cluster note.

Rules kept here:

1. pass^k is printed only where ``k_effective > 1``; otherwise ``= pass^1 (rollouts
   identical)``.
2. In fake mode the rates are plumbing checks, and trap results are harness plumbing; the
   report never prints a fake-mode trap tally.
3. With fewer than two world clusters no interval is printed for the suite, only counts.
4. Cost is steps per passing episode with the median and maximum per rollout beside it.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aksha.evals.metrics import (
    cost_per_pass,
    failure_clusters,
    k_effective,
    pass_at_k,
    pass_hat_k,
    wilson,
)
from aksha.evals.results import SuiteResults

PLUMBING = "fake model mode: rates are plumbing checks, not reliability"


@dataclass(frozen=True)
class CaseRow:
    """One (case, world) row of the case table."""

    case: str
    kind: str
    origin: str
    world: str
    cluster: str
    mode: str
    n: int
    passes: int
    ungradeable: int
    k_eff: int
    pass_at_1: float
    wilson_lo: float
    wilson_hi: float
    pass_hat_k: float | None
    k: int
    steps_per_pass: float
    steps_median: float
    steps_max: int
    seconds: float
    trace: str
    xfail: bool

    def as_json(self) -> dict[str, Any]:
        """The row for ``report.json``."""
        out = dict(self.__dict__)
        for k, v in out.items():
            if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
                out[k] = None
        return out


def case_rows(results: SuiteResults) -> list[CaseRow]:
    """Rollouts of each (case, world) folded into one row."""
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for ep in results.episodes:
        groups[(str(ep["case"]), str(ep["world"]))].append(ep)
    rows = []
    for (case, world), eps in groups.items():
        n = len(eps)
        c = sum(e["verdict"] == "pass" for e in eps)
        keff = k_effective([str(e["trace_hash"]) for e in eps])
        k = min(3, n - 1) if n > 1 else 1
        hat = pass_hat_k(c, n, k) if keff > 1 and n > k else None
        cost = cost_per_pass([int(e["steps"]) for e in eps], [e["verdict"] == "pass" for e in eps])
        lo, hi = wilson(c, n)
        first = eps[0]
        rows.append(
            CaseRow(
                case=case,
                kind=str(first.get("kind", "")),
                origin=str(first.get("origin", "")),
                world=world,
                cluster=str(first.get("cluster", world)),
                mode=str(first.get("mode", "")),
                n=n,
                passes=c,
                ungradeable=sum(e["verdict"] == "ungradeable" for e in eps),
                k_eff=keff,
                pass_at_1=pass_at_k(c, n, 1),
                wilson_lo=lo,
                wilson_hi=hi,
                pass_hat_k=hat,
                k=k,
                steps_per_pass=cost.per_pass,
                steps_median=cost.median,
                steps_max=cost.maximum,
                seconds=sum(int(e.get("elapsed_ms", 0)) for e in eps) / n / 1000,
                trace=str(first.get("trace_path", "")),
                xfail=bool(first.get("xfail", False)),
            )
        )
    return sorted(rows, key=lambda r: (r.case, r.world))


def selftest_line(results: SuiteResults) -> str:
    """The first line: did the self-tests hold, and the mutation coverage."""
    if not results.selftests:
        return "self-tests: not run"
    ok = all(s["ok"] for s in results.selftests)
    killed: set[str] = set()
    musts: set[str] = set()
    for s in results.selftests:
        killed |= set(s.get("killed", ()))
        musts |= set(s.get("must_criteria", ()))
    total = len(musts) or len(killed)
    return (
        f"self-tests: {'pass' if ok else 'FAIL'}; mutation coverage "
        f"{len(killed & musts) if musts else len(killed)} of {total} gradeable criteria"
    )


def verdict(results: SuiteResults) -> str:
    """The suite verdict: ``ungradeable`` beats ``fail`` beats ``pass``; xfail is honoured."""
    selftests_ok = all(s["ok"] for s in results.selftests)
    outcomes = []
    for ep in results.episodes:
        v = ep["verdict"]
        if ep.get("xfail"):
            v = "pass" if v == "fail" else "fail"  # strict: an xfail that passes is a failure
        outcomes.append(v)
    for rel in results.relations:
        outcomes.extend(c["outcome"] for c in rel["criteria"] if c.get("must", True))
    if not selftests_ok or "ungradeable" in outcomes:
        return "ungradeable"
    return "fail" if "fail" in outcomes else "pass"


def _fmt(x: float | None, digits: int = 2) -> str:
    if x is None or math.isnan(x):
        return "n/a"
    if math.isinf(x):
        return "inf"
    return f"{x:.{digits}f}"


def render_text(results: SuiteResults) -> str:
    """``report.txt``."""
    started = results.started or {}
    mode = str(started.get("mode", "fake"))
    lines = [selftest_line(results)]
    lines.append(PLUMBING if mode == "fake" else f"{mode} model mode")
    rows = case_rows(results)
    clusters = sorted({r.cluster for r in rows})
    items = {(str(e["case"]), str(e.get("source", ""))) for e in results.episodes}
    passed_items = {
        (str(e["case"]), str(e.get("source", "")))
        for e in results.episodes
        if all(x["verdict"] == "pass" for x in results.episodes if x["case"] == e["case"])
    }
    lines.append(
        f"suite {started.get('suite', '?')}: {verdict(results)}; unique case items passed: "
        f"{len(passed_items)} of {len(items)} in {len(clusters)} world cluster(s)"
    )
    if len(clusters) < 2:
        lines.append("one world cluster: counts only, no suite-level interval")
    lines.append("")
    lines.append("self-test bundles:")
    for s in results.selftests:
        mark = "ok" if s["ok"] else "FAIL"
        lines.append(f"  {mark:4} {s['world']:14} {s['bundle']}")
        lines.extend(f"         {p}" for p in s.get("problems", ())[:5] if not s["ok"])
    inv = [
        (e["case"], e["world"], c["id"], "; ".join(c.get("evidence", ())[:1]))
        for e in results.episodes
        for c in e["criteria"]
        if c["layer"] == "invariant" and c["outcome"] == "fail"
    ]
    lines.append("")
    lines.append(f"invariant failures: {len(inv)}")
    lines.extend(f"  {c} {w} {cid}: {ev}" for c, w, cid, ev in inv)
    fails = [
        (
            str(c["id"]),
            str(c["layer"]),
            str((c.get("evidence") or [""])[0]),
            f"{e['case']}/{e['world']}/r{e['rollout']}",
        )
        for e in results.episodes
        for c in e["criteria"]
        if c["must"] and c["outcome"] != "pass"
    ]
    clusters_f = failure_clusters(fails)
    lines.append("")
    lines.append(f"failure clusters: {len(clusters_f)}")
    lines.extend(f"  {f.count:3} x {f.signature}  e.g. {', '.join(f.examples)}" for f in clusters_f)
    lines.append("")
    lines.append(
        "case | kind/origin | world | mode | n | pass | pass@1 (Wilson 95%) | pass^k | "
        "ungradeable | steps per pass (median, max) | s per episode | trace"
    )
    for r in rows:
        hat = (
            f"pass^{r.k} {_fmt(r.pass_hat_k)}"
            if r.pass_hat_k is not None
            else "= pass^1 (rollouts identical)"
        )
        p1 = f"{_fmt(r.pass_at_1)} ({_fmt(r.wilson_lo)} to {_fmt(r.wilson_hi)})"
        if r.mode == "fake":
            p1 += " plumbing"
        lines.append(
            f"{r.case}{' (xfail)' if r.xfail else ''} | {r.kind}/{r.origin} | {r.world} | "
            f"{r.mode} | "
            f"{r.n} | {r.passes} | {p1} | {hat} | {r.ungradeable} | "
            f"{_fmt(r.steps_per_pass, 1)} ({_fmt(r.steps_median, 1)}, {r.steps_max}) | "
            f"{_fmt(r.seconds)} | {r.trace}"
        )
    if results.relations:
        lines.append("")
        lines.append("relations:")
        for rel in results.relations:
            for c in rel["criteria"]:
                ev = "; ".join(c.get("evidence", ())[:1])
                lines.append(
                    f"  {rel['case']} {rel['relation']} {rel['base']} -> {rel['variant']} "
                    f"r{rel.get('rollout', 0)}: "
                    f"{c['id']} {c['outcome']} {ev}"
                )
    if mode != "fake":
        traps = [
            c
            for e in results.episodes
            for c in e["criteria"]
            if c["layer"] == "trap" and c["id"].startswith("trap.")
        ]
        handled = sum(c.get("reading") == "handled" for c in traps)
        lines.append("")
        lines.append(
            f"trap handling (live mode only): {handled} of {len(traps)} trap results handled"
        )
    return "\n".join(lines) + "\n"


def write_reports(results: SuiteResults, out: Path) -> dict[str, Path]:
    """Write ``results.jsonl``, ``report.json`` and ``report.txt`` into ``out``."""
    out.mkdir(parents=True, exist_ok=True)
    rows = "".join(json.dumps(e, sort_keys=True) + "\n" for e in results.episodes)
    (out / "results.jsonl").write_text(rows, encoding="utf-8")
    report = {
        "selftest": selftest_line(results),
        "verdict": verdict(results),
        "mode": (results.started or {}).get("mode"),
        "conditions": (results.started or {}).get("conditions", {}),
        "selftests": list(results.selftests),
        "cases": [r.as_json() for r in case_rows(results)],
        "relations": list(results.relations),
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (out / "report.txt").write_text(render_text(results), encoding="utf-8")
    return {n: out / n for n in ("results.jsonl", "report.json", "report.txt")}


def exit_code(results: SuiteResults) -> int:
    """0 when the suite passed, 1 when a case failed, 2 when anything was ungradeable."""
    return {"pass": 0, "fail": 1, "ungradeable": 2}[verdict(results)]
