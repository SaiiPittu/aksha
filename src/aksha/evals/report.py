"""The report: built from the results projection, in the order the eval design sets.

``report.txt`` starts with the self-test line and its mutation coverage, then the mode
label, because a failed self-test makes everything after it untrustworthy. Then the
headline (unique case and trap items passed, with the world cluster count), invariant
failures, failure clusters, the case table, relations and the cluster note.

Rules kept here:

1. pass^k is printed only where ``k_effective > 1``; otherwise ``= pass^1 (rollouts
   identical)``. k is ``min(3, n - 1)`` over n rollouts, so the unbiased estimator still
   averages over subsets, and the row prints the k and n it used (``pass^3 (n=5)``).
2. In fake mode the rates are plumbing checks, and trap results are harness plumbing; the
   report never prints a fake-mode trap tally. Mode is per episode: a run mixing modes
   says so, and the trap tally counts only episodes run with a live or replayed model.
3. With fewer than two world clusters no interval is printed for the suite, only counts.
   With two or more, the suite's pass rate carries a cluster-robust standard error over
   world clusters (:func:`aksha.evals.metrics.clustered_se`) and a 95% interval from it.
4. Cost is steps per passing episode with the median and maximum per rollout beside it.
5. An ``xfail`` case inverts pass and fail only; ``ungradeable`` stays ``ungradeable``.
6. A case with arms gets one row per arm (``case[arm]``), never pooled, and an arms table:
   per arm, the must criteria passed, the agent's measures of its rebuild runs (model
   turns, tool calls, tokens, context characters, re-entries that resumed and why the
   others went fresh) and pass^k, then each compared pair's differences. In fake mode
   the measures are a property of the rule-based fake, and the table says so.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aksha.evals.metrics import (
    clustered_se,
    cost_per_pass,
    failure_clusters,
    k_effective,
    pass_at_k,
    pass_hat_k,
    wilson,
)
from aksha.evals.results import SuiteResults

PLUMBING = "fake model mode: rates are plumbing checks, not reliability"
PLUMBING_MODES = ("fake", "scripted", "selftest")
"""Modes whose rates and trap results only prove the plumbing."""


@dataclass(frozen=True)
class SuiteRate:
    """The suite's pass rate over episodes, with a clustered interval when it has one."""

    episodes: int
    passes: int
    clusters: int
    se: float | None

    @property
    def rate(self) -> float:
        """Passing episodes over gradeable and failing ones; NaN with no episodes."""
        return self.passes / self.episodes if self.episodes else math.nan

    @property
    def interval(self) -> tuple[float, float] | None:
        """``rate`` plus or minus 1.96 clustered standard errors, clipped to [0, 1]."""
        if self.se is None:
            return None
        return max(0.0, self.rate - 1.96 * self.se), min(1.0, self.rate + 1.96 * self.se)

    def as_json(self) -> dict[str, Any]:
        """The rate for ``report.json``."""
        interval = self.interval
        return {
            "episodes": self.episodes,
            "passes": self.passes,
            "clusters": self.clusters,
            "rate": None if math.isnan(self.rate) else self.rate,
            "clustered_se": self.se,
            "interval_95": list(interval) if interval else None,
        }


def suite_rate(results: SuiteResults) -> SuiteRate:
    """Every episode scored 1 for pass, 0 otherwise, clustered by its world cluster."""
    eps = list(results.episodes)
    scores = [1.0 if e["verdict"] == "pass" else 0.0 for e in eps]
    clusters = [str(e.get("cluster", e["world"])) for e in eps]
    se, g = clustered_se(scores, clusters) if eps else (None, 0)
    return SuiteRate(len(eps), int(sum(scores)), g, se)


def run_mode(results: SuiteResults) -> str:
    """The run's model mode: the one every episode used, or ``mixed``."""
    modes = {str(e.get("mode", "")) for e in results.episodes}
    if len(modes) == 1:
        return modes.pop()
    if not modes:
        return str((results.started or {}).get("mode", "fake"))
    return "mixed"


@dataclass(frozen=True)
class CaseRow:
    """One (case, world) row of the case table."""

    case: str
    kind: str
    origin: str
    world: str
    arm: str
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
    k_requested: int
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
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for ep in results.episodes:
        groups[(str(ep["case"]), arm_name(ep), str(ep["world"]))].append(ep)
    rows = []
    for (case, arm, world), eps in groups.items():
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
                arm=arm,
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
                k_requested=int(dict(first.get("conditions", {})).get("k", n)),
                steps_per_pass=cost.per_pass,
                steps_median=cost.median,
                steps_max=cost.maximum,
                seconds=sum(int(e.get("elapsed_ms", 0)) for e in eps) / n / 1000,
                trace=str(first.get("trace_path", "")),
                xfail=bool(first.get("xfail", False)),
            )
        )
    return sorted(rows, key=lambda r: (r.case, r.world, r.arm))


def arm_name(episode: Mapping[str, Any]) -> str:
    """The arm an episode row ran under; empty for a case without arms."""
    arm = episode.get("arm")
    return str(arm.get("name", "")) if isinstance(arm, Mapping) else ""


MEASURES = ("model_turns", "tool_calls", "input_tokens", "output_tokens", "context_chars")
"""The agent's measures of its rebuild runs the arms table prints, in order."""


def arm_rows(results: SuiteResults) -> list[dict[str, Any]]:
    """One row per (case, world, arm) of a case with arms: correctness, measures, pass^k."""
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for ep in results.episodes:
        if arm_name(ep):
            groups[(str(ep["case"]), str(ep["world"]), arm_name(ep))].append(ep)
    rows: list[dict[str, Any]] = []
    for (case, world, arm), eps in groups.items():
        n = len(eps)
        c = sum(e["verdict"] == "pass" for e in eps)
        keff = k_effective([str(e["trace_hash"]) for e in eps])
        k = min(3, n - 1) if n > 1 else 1
        musts = [x for e in eps for x in e["criteria"] if x["must"]]
        rebuilt = [dict(dict(e.get("metrics", {})).get("rebuild", {})) for e in eps]
        measures = {m: [int(r.get(m, 0)) for r in rebuilt] for m in MEASURES}
        modes: dict[str, int] = defaultdict(int)
        reasons: dict[str, int] = defaultdict(int)
        for r in rebuilt:
            for mode, count in dict(r.get("reentry_modes", {})).items():
                modes[str(mode)] += int(count)
            for reason, count in dict(r.get("fresh_reasons", {})).items():
                reasons[str(reason)] += int(count)
        rows.append(
            {
                "case": case,
                "world": world,
                "arm": arm,
                "settings": dict(eps[0]["arm"]),
                "mode": str(eps[0].get("mode", "")),
                "n": n,
                "passes": c,
                "verdicts": [str(e["verdict"]) for e in eps],
                "must_passed": sum(x["outcome"] == "pass" for x in musts),
                "must_total": len(musts),
                "k_eff": keff,
                "pass_hat_k": pass_hat_k(c, n, k) if keff > 1 and n > k else None,
                "k": k,
                "measures_median": {
                    m: statistics.median(v) if v else 0 for m, v in measures.items()
                },
                "reentry_modes": dict(sorted(modes.items())),
                "fresh_reasons": dict(sorted(reasons.items())),
                "definition_hash": str(
                    dict(eps[0].get("conditions", {})).get("agent_definition_hash", "")
                ),
            }
        )
    return sorted(rows, key=lambda r: (r["case"], r["world"], r["arm"]))


def _arm_lines(results: SuiteResults) -> list[str]:
    rows = arm_rows(results)
    if not rows:
        return []
    lines = ["", "arms (measures are medians over the arm's rollouts, of its rebuild runs):"]
    if any(r["mode"] in PLUMBING_MODES for r in rows):
        lines.append(
            "  fake model mode: turns, tool calls, tokens and context size are properties of "
            "the rule-based fake, not of a model; a fake that reports no usage counts 0 tokens"
        )
    lines.append(
        "  case | world | arm | reentry | premise exempt | verdict | must passed | "
        "model turns | tool calls | input/output tokens | context chars | re-entries | "
        "fresh reasons (re-entries and reasons over all rollouts) | pass^k"
    )
    by_key = {(r["case"], r["world"], r["arm"]): r for r in rows}
    for r in rows:
        m, s = r["measures_median"], r["settings"]
        if r["pass_hat_k"] is not None:
            hat = f"pass^{r['k']} (n={r['n']}) {_fmt(r['pass_hat_k'])}"
        elif r["k_eff"] > 1:
            hat = f"n/a (n={r['n']}, too few rollouts)"
        else:
            hat = f"= pass^1 (n={r['n']}, rollouts identical)"
        modes = ", ".join(f"{k} {v}" for k, v in r["reentry_modes"].items()) or "none"
        reasons = ", ".join(f"{k} {v}" for k, v in r["fresh_reasons"].items()) or "none"
        lines.append(
            f"  {r['case']} | {r['world']} | {r['arm']} | {s.get('reentry')} | "
            f"{', '.join(s.get('premise_exempt', ())) or '-'} | "
            f"{'/'.join(sorted(set(r['verdicts'])))} ({r['passes']} of {r['n']}) | "
            f"{r['must_passed']} of {r['must_total']} | {_fmt(m['model_turns'], 0)} | "
            f"{_fmt(m['tool_calls'], 0)} | {_fmt(m['input_tokens'], 0)}/"
            f"{_fmt(m['output_tokens'], 0)} | {_fmt(m['context_chars'], 0)} | {modes} | "
            f"{reasons} | {hat}"
        )
    for r in rows:
        for a, b in r["settings"].get("compare", ()):
            if a != r["arm"]:
                continue
            other = by_key.get((r["case"], r["world"], b))
            if other is None:
                continue
            diffs = ", ".join(
                f"{name} {_signed(r['measures_median'][name] - other['measures_median'][name])}"
                for name in MEASURES
            )
            lines.append(f"  {r['case']} {r['world']}: {a} against {b}: {diffs}")
    return lines


def _signed(x: float) -> str:
    return f"{x:+.0f}"


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
        if ep.get("xfail") and v != "ungradeable":
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
    mode = run_mode(results)
    lines = [selftest_line(results)]
    if mode in PLUMBING_MODES:
        lines.append(PLUMBING)
    elif mode == "mixed":
        lines.append("mixed model modes: rows marked plumbing are fake-mode checks")
    else:
        lines.append(f"{mode} model mode")
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
    rate = suite_rate(results)
    if rate.interval is None:
        lines.append(f"{rate.clusters} world cluster(s): counts only, no suite-level interval")
    else:
        lo, hi = rate.interval
        lines.append(
            f"suite pass rate {_fmt(rate.rate)} (95% {_fmt(lo)} to {_fmt(hi)}, clustered SE "
            f"{_fmt(rate.se, 3)} over {rate.clusters} world clusters)"
        )
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
        if r.pass_hat_k is not None:
            hat = f"pass^{r.k} (n={r.n}) {_fmt(r.pass_hat_k)}"
        elif r.k_eff > 1:
            hat = f"n/a (n={r.n}, too few rollouts)"
        else:
            hat = "= pass^1 (rollouts identical)"
        p1 = f"{_fmt(r.pass_at_1)} ({_fmt(r.wilson_lo)} to {_fmt(r.wilson_hi)})"
        if r.mode in PLUMBING_MODES:
            p1 += " plumbing"
        name = f"{r.case}[{r.arm}]" if r.arm else r.case
        lines.append(
            f"{name}{' (xfail)' if r.xfail else ''} | {r.kind}/{r.origin} | {r.world} | "
            f"{r.mode} | "
            f"{r.n} | {r.passes} | {p1} | {hat} | {r.ungradeable} | "
            f"{_fmt(r.steps_per_pass, 1)} ({_fmt(r.steps_median, 1)}, {r.steps_max}) | "
            f"{_fmt(r.seconds)} | {r.trace}"
        )
    lines.extend(_arm_lines(results))
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
    traps = [
        c
        for e in results.episodes
        if str(e.get("mode", "")) not in PLUMBING_MODES
        for c in e["criteria"]
        if c["layer"] == "trap" and c["id"].startswith("trap.")
    ]
    if traps:
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
        "mode": run_mode(results),
        "suite_rate": suite_rate(results).as_json(),
        "conditions": (results.started or {}).get("conditions", {}),
        "selftests": list(results.selftests),
        "cases": [r.as_json() for r in case_rows(results)],
        "relations": list(results.relations),
        "arms": arm_rows(results),
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (out / "report.txt").write_text(render_text(results), encoding="utf-8")
    return {n: out / n for n in ("results.jsonl", "report.json", "report.txt")}


def exit_code(results: SuiteResults) -> int:
    """0 when the suite passed, 1 when a case failed, 2 when anything was ungradeable."""
    return {"pass": 0, "fail": 1, "ungradeable": 2}[verdict(results)]
