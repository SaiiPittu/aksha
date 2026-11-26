"""Run the self-tests for one world and report what each bundle proved.

:func:`self_test` builds the oracle, null, lister and hedger bundles through ordinary
episodes, derives the perturbed oracles and the mutants from the oracle's bundle, grades
each against the world's key with every criterion, and checks the expectations in the
package docstring. A criterion the oracle cannot grade because its key field is missing
(``key_ref_missing``) is reported as a key gap, not a failure; a mutant aimed at it is
reported as not applicable.

:func:`relation_self_test` proves the relations can fail: the oracle bundles of a base
world and its variant must pass ``rel.directional``, and a variant whose changed candidate
was left as in the base must fail it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from aksha.evals.bundle import EpisodeRecord
from aksha.evals.case import EvalCase
from aksha.evals.criteria.forbidden import NEGATIVE_SETS, violations
from aksha.evals.environment import Environment
from aksha.evals.grade import Criterion, Verdict, grade
from aksha.evals.key import AnswerKey
from aksha.evals.ports import AgentUnderTest, StoreOpener
from aksha.evals.relations import directional, key_delta
from aksha.evals.selftest.bundles import (
    PERTURBATIONS,
    HedgerAgent,
    ListerAgent,
    NullAgent,
    OracleAgent,
    Perturb,
)
from aksha.evals.selftest.mutants import MUTANTS
from aksha.evals.state import fold
from aksha.evals.world import Renaming, World

LISTER_MUST_FAIL = ("research.competitor_count", "patent.matrix", "trap.T-DUP")
HEDGER_MUST_FAIL = ("precision.known_values",)


@dataclass(frozen=True)
class BundleCheck:
    """What one self-test bundle proved: ``ok`` and, when not, why."""

    bundle: str
    ok: bool
    problems: tuple[str, ...] = ()
    killed: tuple[str, ...] = ()


@dataclass
class SelfTestReport:
    """Every bundle check for one world, the key gaps and the mutation coverage."""

    world: str
    checks: list[BundleCheck] = field(default_factory=list)
    key_gaps: list[str] = field(default_factory=list)
    criteria: tuple[str, ...] = ()
    oracle: EpisodeRecord | None = None

    @property
    def ok(self) -> bool:
        """Whether every bundle check held."""
        return all(c.ok for c in self.checks)

    @property
    def killed(self) -> set[str]:
        """Criteria some mutant was caught by."""
        return {k for c in self.checks for k in c.killed}

    @property
    def coverage(self) -> tuple[int, int]:
        """(criteria killed by a mutant, criteria the oracle could grade)."""
        gradeable = [c for c in self.criteria if c not in self.key_gaps]
        return len(self.killed & set(gradeable)), len(gradeable)


def selftest_case(world: World, key: AnswerKey, registry: Mapping[str, Criterion]) -> EvalCase:
    """The synthetic case the self-tests grade with: every criterion, every anchored fact."""
    expect = [f"facts.{f.id}" for f in key.facts.values() if f.anchor] + [
        f"traps.{t}" for t in key.traps
    ]
    return EvalCase(
        id="selftest",
        kind="control",
        origin="hand_written",
        source="requirement: grader self-tests",
        category="robustness",
        agents=("research", "patent"),
        worlds=(world.name,),
        expect=tuple(expect),
        criteria=tuple(registry),
    )


def _run(env: Environment, case: EvalCase, agent: AgentUnderTest, name: str) -> EpisodeRecord:
    h = env.reset(case, 0, agent=name, mode="selftest")
    env.run(h, agent)
    return env.finalize(h)


def _outcomes(v: Verdict) -> dict[str, str]:
    return {r.id: r.outcome for r in v.results}


def _perturbed(fn: Perturb) -> Callable[[list[dict[str, Any]]], list[dict[str, Any]]]:
    def edit(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {**e, "payload": {**e["payload"], "content": fn(e["payload"]["content"])}}
            if e["type"] == "episode.output_saved"
            else e
            for e in events
        ]

    return edit


def _with(case: EvalCase, case_id: str) -> EvalCase:
    return replace(case, id=case_id)


def self_test(
    world: World,
    key: AnswerKey,
    *,
    open_store: StoreOpener,
    out: Path,
    registry: Mapping[str, Criterion],
    cases: Sequence[EvalCase] = (),
) -> SelfTestReport:
    """Run every self-test bundle for ``world`` (module docstring).

    With ``cases`` (the suite's cases that list this world), the null bundle is also graded
    as each case: a case with ``needs_action`` must fail it, and one without must not, so
    a case whose criteria an empty episode passes is caught before it is trusted.
    """
    env = Environment(world, open_store=open_store, out=out)
    case = selftest_case(world, key, registry)
    report = SelfTestReport(world=world.name, criteria=tuple(registry))

    def graded(rec: EpisodeRecord) -> Verdict:
        return grade(rec, case, key, world, registry)

    oracle = _run(env, _with(case, "selftest-oracle"), OracleAgent(key, world), "oracle")
    report.oracle = oracle
    base = graded(oracle)
    report.key_gaps = sorted(r.id for r in base.results if r.cause == "key_ref_missing")
    bad = [
        f"{r.id}: {r.outcome} {r.cause or ''} {'; '.join(r.evidence[:2])}".strip()
        for r in base.results
        if r.outcome != "pass" and r.cause != "key_ref_missing"
    ]
    report.checks.append(BundleCheck("oracle", not bad, tuple(bad)))
    want = _outcomes(base)

    for name, fn in PERTURBATIONS.items():
        rec = oracle.edited(events=_perturbed(fn))
        got = _outcomes(graded(rec))
        diff = [f"{c}: {got.get(c)} (oracle {o})" for c, o in want.items() if got.get(c) != o]
        report.checks.append(BundleCheck(f"perturbed:{name}", not diff, tuple(diff)))

    null_rec = _run(env, _with(case, "selftest-null"), NullAgent(), "null")
    null = graded(null_rec)
    inv = [r for r in null.results if r.layer == "invariant"]
    problems = [f"{r.id}: {r.outcome}" for r in inv if r.outcome != "pass"]
    if null.outcome != "fail":
        problems.append(f"null verdict is {null.outcome}, want fail")
    report.checks.append(BundleCheck("null", not problems, tuple(problems)))
    if cases:
        problems = []
        for c in cases:
            outcome = grade(null_rec, c, key, world, registry).outcome
            if c.needs_action and outcome != "fail":
                problems.append(f"{c.id}: the null bundle is {outcome}, want fail (needs_action)")
            elif not c.needs_action and outcome == "fail":
                problems.append(f"{c.id}: the null bundle fails a case without needs_action")
        report.checks.append(BundleCheck("null-per-case", not problems, tuple(problems)))

    for name, agent, must_fail in (
        ("lister", ListerAgent(key, world), LISTER_MUST_FAIL),
        ("hedger", HedgerAgent(key, world), HEDGER_MUST_FAIL),
    ):
        v = graded(_run(env, _with(case, f"selftest-{name}"), agent, name))
        got = _outcomes(v)
        missed = [f"{c}: {got.get(c)}, want fail" for c in must_fail if got.get(c) != "fail"]
        report.checks.append(BundleCheck(name, not missed, tuple(missed)))

    for name, mutant in MUTANTS.items():
        applicable = [c for c in mutant.kills if want.get(c) == "pass"]
        if not applicable:
            report.checks.append(BundleCheck(f"mutant:{name}", True, ("not applicable: key gap",)))
            continue
        got = _outcomes(graded(mutant.plant(oracle, key)))
        survived = [f"{c}: {got.get(c)}, want fail" for c in applicable if got.get(c) != "fail"]
        killed = tuple(c for c in applicable if got.get(c) == "fail")
        report.checks.append(BundleCheck(f"mutant:{name}", not survived, tuple(survived), killed))

    problems = []
    for check, (catch, allow) in NEGATIVE_SETS.items():
        problems += [f"{check} missed {t!r}" for t in catch if not violations(check, t)]
        problems += [f"{check} flagged {t!r}" for t in allow if violations(check, t)]
    report.checks.append(BundleCheck("forbidden-negative-sets", not problems, tuple(problems)))
    return report


def relation_self_test(
    base: EpisodeRecord,
    variant: EpisodeRecord,
    base_key: AnswerKey,
    variant_key: AnswerKey,
    maps: Renaming,
) -> BundleCheck:
    """The oracles pass ``rel.directional``; a variant that ignored the change fails it."""
    delta = key_delta(base_key, variant_key)
    bs, vs = fold(base.events), fold(variant.events)
    good = directional(bs, vs, maps, delta=delta)[0]
    problems = [] if good.outcome == "pass" else [f"oracle pair: {e}" for e in good.evidence[:5]]
    if delta.must_move:
        stale = fold(variant.events)
        for kind, key in delta.must_move:
            out = bs.get(kind, key)
            if out is not None:
                stale.outputs[out.ref] = replace(
                    out,
                    content=maps.apply(dict(out.content)),
                    citations=tuple(maps.apply(list(out.citations))),
                )
        bad = directional(bs, stale, maps, delta=delta)[0]
        if bad.outcome != "fail":
            problems.append("a variant that ignored the change passed rel.directional")
    else:
        problems.append("the keys name no change: the pair cannot test rel.directional")
    killed = ("rel.directional",) if not problems else ()
    return BundleCheck("relations", not problems, tuple(problems), killed)
