"""The suite runner: self-tests, k rollouts per case and world, relations, the report.

:func:`run_suite` checks every case against its worlds' keys, runs the grader self-tests
for each world first (their result is the report's first line), then runs each case k
times in each of its worlds through the registered agent, grades every episode and writes
each verdict as an event in the results store. Cases with ``relation = "invariance"`` or
``"directional"`` are then compared rollout by rollout between each variant world and its
base. The report is rebuilt from the results store's projection.

Run conditions go on every result row: model mode, seed, k, fault profile digest, world
lock hash, grader versions, the agent's registration and the runtime commit with a dirty
flag when the caller supplies them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aksha.evals.bundle import EpisodeRecord
from aksha.evals.case import EvalCase, Suite, check_against_keys
from aksha.evals.criteria import REGISTRY
from aksha.evals.environment import Environment
from aksha.evals.errors import CaseInvalid
from aksha.evals.grade import Criterion, Verdict, grade, order
from aksha.evals.judge import JUDGE_CRITERIA, Judge
from aksha.evals.key import AnswerKey, load_key
from aksha.evals.ports import StoreOpener
from aksha.evals.registry import AgentRegistry
from aksha.evals.relations import directional, invariance, key_delta
from aksha.evals.report import exit_code, write_reports
from aksha.evals.results import ResultsWriter, SuiteResults, criteria_json, project_results
from aksha.evals.selftest.run import relation_self_test, self_test
from aksha.evals.state import fold
from aksha.evals.world import World, load_world, lock_digest, renaming


@dataclass
class SuiteRun:
    """What one run produced: the projection, the report files and the exit code."""

    results: SuiteResults
    out: Path
    reports: Mapping[str, Path]
    exit_code: int
    records: dict[tuple[str, str, int], EpisodeRecord] = field(default_factory=dict)
    verdicts: dict[tuple[str, str, int], Verdict] = field(default_factory=dict)


def validate_suite(
    suite: Suite,
    keys: Mapping[str, AnswerKey],
    registry: Mapping[str, Criterion] = REGISTRY,
) -> list[str]:
    """Refuse unknown criteria and broken traps; return the key-gap warnings."""
    warnings: list[str] = []
    for case in suite.cases:
        order([*case.criteria, *case.should], registry)
        for j in case.judge:
            if j not in JUDGE_CRITERIA:
                raise CaseInvalid("unknown_criterion", f"{case.id}: no judge criterion {j!r}")
        for problem in check_against_keys(case, keys):
            if problem.startswith("missing:"):
                warnings.append(problem)
            else:
                raise CaseInvalid(message=problem)
    return warnings


def _conditions(
    case: EvalCase,
    world: World,
    suite: Suite,
    *,
    seed: int,
    k: int,
    mode: str,
    extra: Mapping[str, Any],
) -> dict[str, Any]:
    faults = suite.faults.get(case.faults) if case.faults else None
    versions = {c: REGISTRY[c].version for c in (*case.criteria, *case.should)}
    return {
        "mode": mode,
        "seed": seed,
        "k": k,
        "fault_profile": faults.digest if faults else None,
        "world_lock": lock_digest(world),
        "grader_versions": versions,
        **dict(extra),
    }


def run_suite(
    suite: Suite,
    *,
    fixtures_root: Path,
    agents: AgentRegistry,
    open_store: StoreOpener,
    out: Path,
    seed: int = 0,
    k: int | None = None,
    judge: Judge | None = None,
    selftests: bool = True,
    cases: Sequence[str] = (),
    agent_override: str | None = None,
    conditions: Mapping[str, Any] | None = None,
) -> SuiteRun:
    """Run ``suite`` and write its results and reports into ``out`` (module docstring)."""
    worlds = {w: load_world(w, fixtures_root) for w in suite.worlds}
    keys = {w: load_key(world) for w, world in worlds.items()}
    validate_suite(suite, keys)
    chosen = [suite.case(c) for c in cases] if cases else list(suite.cases)
    mode = "fake"
    for case in chosen:
        name = agent_override or case.agents[0]
        mode = agents.get(name).mode
    writer = ResultsWriter(open_store, out, suite.name)
    records: dict[tuple[str, str, int], EpisodeRecord] = {}
    verdicts: dict[tuple[str, str, int], Verdict] = {}
    try:
        writer.suite_started(
            {
                "suite": suite.name,
                "seed": seed,
                "mode": mode,
                "agents": {c.id: agent_override or c.agents[0] for c in chosen},
                "conditions": dict(conditions or {}),
            }
        )
        if selftests:
            _selftests(writer, worlds, keys, open_store, out / "_selftest")
        for case in chosen:
            name = agent_override or case.agents[0]
            reg = agents.get(name)
            rollouts = k or case.k or suite.k
            for wname in case.worlds:
                world = worlds[wname]
                env = Environment(
                    world, open_store=open_store, out=out, seed=seed, faults=dict(suite.faults)
                )
                for r in range(rollouts):
                    h = env.reset(case, r, agent=name, mode=reg.mode)
                    env.run(h, reg.factory())
                    rec = env.finalize(h)
                    v = grade(rec, case, keys[wname], world, REGISTRY, judge=judge)
                    records[(case.id, wname, r)] = rec
                    verdicts[(case.id, wname, r)] = v
                    writer.episode(
                        {
                            "case": case.id,
                            "world": wname,
                            "cluster": world.cluster,
                            "rollout": r,
                            "agent": name,
                            "mode": reg.mode,
                            "kind": case.kind,
                            "origin": case.origin,
                            "source": case.source,
                            "category": case.category,
                            "verdict": v.outcome,
                            "xfail": case.xfail,
                            "criteria": criteria_json(v.results),
                            "steps": int(rec.meta.get("steps", 0)),
                            "elapsed_ms": int(rec.meta.get("elapsed_ms", 0)),
                            "trace_hash": rec.trace_hash,
                            "bundle_path": _rel(rec.path, out),
                            "trace_path": _rel(rec.path / "trace.jsonl" if rec.path else None, out),
                            "conditions": _conditions(
                                case,
                                world,
                                suite,
                                seed=seed,
                                k=rollouts,
                                mode=reg.mode,
                                extra=conditions or {},
                            ),
                        }
                    )
            if case.relation != "none":
                _relations(writer, case, worlds, keys, records, verdicts, rollouts)
        results_so_far = project_results(writer.store, suite.name)
        from aksha.evals.report import verdict as suite_verdict

        writer.suite_ended({"episodes": len(records), "verdict": suite_verdict(results_so_far)})
        results = project_results(writer.store, suite.name)
    finally:
        writer.close()
    reports = write_reports(results, out)
    return SuiteRun(results, out, reports, exit_code(results), records, verdicts)


def _rel(path: Path | None, out: Path) -> str:
    if path is None:
        return ""
    try:
        return os.path.relpath(path, out)
    except ValueError:
        return str(path)


def _selftests(
    writer: ResultsWriter,
    worlds: Mapping[str, World],
    keys: Mapping[str, AnswerKey],
    open_store: StoreOpener,
    out: Path,
) -> None:
    oracles: dict[str, EpisodeRecord] = {}
    for wname, world in worlds.items():
        report = self_test(world, keys[wname], open_store=open_store, out=out, registry=REGISTRY)
        if report.oracle is not None:
            oracles[wname] = report.oracle
        gradeable = sorted(c for c in report.criteria if c not in report.key_gaps)
        for check in report.checks:
            writer.selftest(
                {
                    "world": wname,
                    "bundle": check.bundle,
                    "ok": check.ok,
                    "problems": list(check.problems[:20]),
                    "killed": list(check.killed),
                    "must_criteria": gradeable,
                }
            )
        if report.key_gaps:
            writer.selftest(
                {
                    "world": wname,
                    "bundle": "key-gaps",
                    "ok": True,
                    "problems": [
                        f"ungradeable until the key gains it: {g}" for g in report.key_gaps
                    ],
                    "killed": [],
                    "must_criteria": gradeable,
                }
            )
    for wname, world in worlds.items():
        base = world.variant_of
        if base and base in worlds and base in oracles and wname in oracles:
            maps = renaming(worlds[base], world)
            check = relation_self_test(oracles[base], oracles[wname], keys[base], keys[wname], maps)
            writer.selftest(
                {
                    "world": wname,
                    "bundle": check.bundle,
                    "ok": check.ok,
                    "problems": list(check.problems),
                    "killed": list(check.killed),
                    "must_criteria": [],
                }
            )


def _relations(
    writer: ResultsWriter,
    case: EvalCase,
    worlds: Mapping[str, World],
    keys: Mapping[str, AnswerKey],
    records: Mapping[tuple[str, str, int], EpisodeRecord],
    verdicts: Mapping[tuple[str, str, int], Verdict],
    rollouts: int,
) -> None:
    for wname in case.worlds:
        world = worlds[wname]
        base = world.variant_of
        if not base or base not in case.worlds:
            continue
        maps = renaming(worlds[base], world)
        delta = key_delta(keys[base], keys[wname])
        for r in range(rollouts):
            b, v = records[(case.id, base, r)], records[(case.id, wname, r)]
            bs, vs = fold(b.events), fold(v.events)
            if case.relation == "invariance":
                results = invariance(
                    b,
                    v,
                    bs,
                    vs,
                    maps,
                    changed=set(delta.changed),
                    base_verdict=verdicts[(case.id, base, r)].outcome,
                    variant_verdict=verdicts[(case.id, wname, r)].outcome,
                )
            else:
                results = directional(bs, vs, maps, delta=delta)
            writer.relation(
                {
                    "case": case.id,
                    "relation": case.relation,
                    "base": base,
                    "variant": wname,
                    "rollout": r,
                    "criteria": criteria_json(results),
                }
            )


def rebuild_report(open_store: StoreOpener, out: Path, suite: str) -> tuple[SuiteResults, int]:
    """Rebuild the report files of an earlier run from its results store."""
    from aksha.evals.events import results_catalogue
    from aksha.evals.results import RESULTS_FILE

    store = open_store(out / RESULTS_FILE, results_catalogue())
    try:
        results = project_results(store, suite)
    finally:
        store.close()
    write_reports(results, out)
    return results, exit_code(results)
