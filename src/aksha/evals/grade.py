"""The grader cascade: gate, invariants, state, citations, traps, then the optional judge.

Every layer runs on every episode, so a double send shows even in a case about prices; the
order decides which failure the verdict names first. The judge runs only when every
``must`` criterion passed, and annotates unless its criterion is calibrated.

Outcomes are closed sets (eval design section 4). A criterion that cannot run is
``ungradeable`` with one of the causes in :data:`UNGRADEABLE_CAUSES`, never a pass or a
fail: a key field the check needs raises :class:`~aksha.evals.errors.KeyRefMissing` and
becomes ``key_ref_missing``; any other exception becomes ``grader_error`` with its type.

Episode verdict: ``pass`` when every ``must`` criterion passes; ``fail`` when one fails;
``ungradeable`` when the gate could not run, or a ``must`` criterion is ungradeable and
none failed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from aksha.evals.bundle import EpisodeRecord
from aksha.evals.case import EvalCase
from aksha.evals.errors import CaseInvalid, KeyRefMissing
from aksha.evals.key import AnswerKey
from aksha.evals.state import FoldError, GradedState, fold
from aksha.evals.world import World

Layer = Literal["gate", "invariant", "state", "citation", "trap", "judge", "relation"]
Outcome = Literal["pass", "fail", "ungradeable"]
Reading = Literal["handled", "missed", "fell_for_it"]
UNGRADEABLE_CAUSES = (
    "bundle_unreadable",
    "store_fold_failed",
    "world_lock_mismatch",
    "key_ref_missing",
    "output_unparseable",
    "grader_error",
    "judge_unavailable",
    "judge_unknown",
)
LAYER_ORDER: tuple[Layer, ...] = ("gate", "invariant", "state", "citation", "trap", "judge")
EVIDENCE_CAP = 12
"""At most this many evidence lines per criterion are kept on a result row."""


@dataclass(frozen=True)
class Check:
    """What one check found, before the cascade adds its ID, layer and ``must``."""

    outcome: Outcome
    evidence: tuple[str, ...] = ()
    cause: str | None = None
    reading: Reading | None = None
    fabricated: bool = False


def passed(*evidence: str, reading: Reading | None = None) -> Check:
    """A passing check."""
    return Check("pass", tuple(evidence), reading=reading)


def failed(*evidence: str, reading: Reading | None = None, fabricated: bool = False) -> Check:
    """A failing check; a trap check says whether the agent missed or fell for it."""
    return Check("fail", tuple(evidence), reading=reading, fabricated=fabricated)


def ungradeable(cause: str, *evidence: str) -> Check:
    """A check that could not run, with a cause from :data:`UNGRADEABLE_CAUSES`."""
    if cause not in UNGRADEABLE_CAUSES:
        raise ValueError(f"unknown ungradeable cause {cause!r}")
    return Check("ungradeable", tuple(evidence), cause=cause)


@dataclass(frozen=True)
class CriterionResult:
    """One criterion's result on one episode (eval design section 14)."""

    id: str
    layer: Layer
    must: bool
    outcome: Outcome
    evidence: tuple[str, ...] = ()
    cause: str | None = None
    reading: Reading | None = None
    fabricated: bool = False
    version: str = "1"

    def as_json(self) -> dict[str, object]:
        """The result as a results-store payload item."""
        return {
            "id": self.id,
            "layer": self.layer,
            "must": self.must,
            "outcome": self.outcome,
            "cause": self.cause,
            "reading": self.reading,
            "fabricated": self.fabricated,
            "evidence": list(self.evidence[:EVIDENCE_CAP]),
        }


@dataclass
class GradeContext:
    """What a check reads besides the bundle: the key, the world and the folded state."""

    key: AnswerKey
    world: World
    state: GradedState
    rec: EpisodeRecord
    case_agents: tuple[str, ...] = ()
    _texts: dict[str, list[str]] = field(default_factory=dict)

    def file_texts(self, rel: str) -> list[str]:
        """Every version of a source file the episode saw: each read, then the final copy."""
        if rel in self._texts:
            return self._texts[rel]
        out: list[str] = []
        for call in self.state.tool_calls:
            if call.get("tool") == "read_source" and call.get("args", {}).get("path") == rel:
                text = self.rec.reads.get(str(call.get("result_hash")))
                if text is not None and text not in out:
                    out.append(text)
        final = self.rec.sources / rel
        if final.is_file() and final.resolve().is_relative_to(self.rec.sources.resolve()):
            text = final.read_text(encoding="utf-8")
            if text not in out:
                out.append(text)
        self._texts[rel] = out
        return out


CheckFn = Callable[[EpisodeRecord, GradeContext, EvalCase], Check]


@dataclass(frozen=True)
class Criterion:
    """A registered criterion: its ID, layer, version and check function."""

    id: str
    layer: Layer
    check: CheckFn
    version: str = "1"
    description: str = ""

    def grade(self, rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> CriterionResult:
        """Run the check, turning a missing key field or an exception into ``ungradeable``."""
        try:
            found = self.check(rec, ctx, case)
        except KeyRefMissing as e:
            found = ungradeable("key_ref_missing", e.message)
        except Exception as e:  # a grader bug is never a model failure
            found = ungradeable("grader_error", f"{type(e).__name__}: {e}"[:300])
        return CriterionResult(
            id=self.id,
            layer=self.layer,
            must=False,
            outcome=found.outcome,
            evidence=found.evidence,
            cause=found.cause,
            reading=found.reading,
            fabricated=found.fabricated,
            version=self.version,
        )


@dataclass(frozen=True)
class Verdict:
    """An episode's verdict and every criterion result, in cascade order."""

    outcome: Outcome
    results: tuple[CriterionResult, ...]
    cause: str | None = None

    @property
    def first_failure(self) -> CriterionResult | None:
        """The first ``must`` criterion that failed, in cascade order."""
        return next((r for r in self.results if r.must and r.outcome == "fail"), None)

    @property
    def must_passed(self) -> tuple[int, int]:
        """(passed, total) over ``must`` criteria: the diagnostic beside the verdict."""
        musts = [r for r in self.results if r.must and r.layer != "gate"]
        return sum(r.outcome == "pass" for r in musts), len(musts)

    def result(self, criterion: str) -> CriterionResult | None:
        """The result for ``criterion``, or None when it did not run."""
        return next((r for r in self.results if r.id == criterion), None)


def gate(rec: EpisodeRecord) -> tuple[list[CriterionResult], GradedState | None]:
    """The gate: the store's chain held, the events fold, the world lock matched."""
    results: list[CriterionResult] = []

    def add(cid: str, c: Check) -> None:
        results.append(
            CriterionResult(cid, "gate", True, c.outcome, c.evidence, c.cause, version="1")
        )

    state: GradedState | None = None
    if not rec.meta.get("store_verified", False):
        add("gate.store", ungradeable("store_fold_failed", str(rec.meta.get("store_break"))))
    else:
        try:
            state = fold(rec.events)
            add("gate.store", passed(f"{len(rec.events)} events folded"))
        except FoldError as e:
            add("gate.store", ungradeable("store_fold_failed", str(e)))
    problems = list(rec.meta.get("world_lock_problems", ()))
    if problems:
        add("gate.world_lock", ungradeable("world_lock_mismatch", *problems))
    else:
        add("gate.world_lock", passed(str(rec.meta.get("world_lock", ""))))
    ok = all(r.outcome == "pass" for r in results)
    return results, state if ok else None


def unreadable(cause: str, detail: str) -> Verdict:
    """The verdict of a bundle that could not even be read."""
    r = CriterionResult("gate.bundle", "gate", True, "ungradeable", (detail,), cause)
    return Verdict("ungradeable", (r,), cause)


def order(ids: Iterable[str], registry: Mapping[str, Criterion]) -> list[str]:
    """Criterion IDs in cascade order (stable within a layer); unknown IDs are refused."""
    known = []
    for cid in ids:
        if cid not in registry:
            raise CaseInvalid("unknown_criterion", f"no criterion {cid!r}")
        known.append(cid)
    return sorted(known, key=lambda c: LAYER_ORDER.index(registry[c].layer))


def grade(
    rec: EpisodeRecord,
    case: EvalCase,
    key: AnswerKey,
    world: World,
    registry: Mapping[str, Criterion],
    *,
    judge: Callable[[EpisodeRecord, GradeContext, EvalCase], Sequence[CriterionResult]]
    | None = None,
) -> Verdict:
    """Grade one episode against its world's key, layer by layer (module docstring)."""
    gate_results, state = gate(rec)
    if state is None:
        cause = next(r.cause for r in gate_results if r.outcome != "pass")
        return Verdict("ungradeable", tuple(gate_results), cause)
    ctx = GradeContext(key=key, world=world, state=state, rec=rec, case_agents=case.agents)
    results = list(gate_results)
    must = set(case.criteria)
    for cid in order([*case.criteria, *case.should], registry):
        r = registry[cid].grade(rec, ctx, case)
        results.append(replace(r, must=cid in must))
    musts = [r for r in results if r.must and r.layer != "gate"]
    if any(r.outcome == "fail" for r in musts):
        outcome: Outcome = "fail"
    elif any(r.outcome == "ungradeable" for r in musts):
        outcome = "ungradeable"
    else:
        outcome = "pass"
    if outcome == "pass" and case.judge and judge is not None:
        judged = list(judge(rec, ctx, case))
        results.extend(judged)
        if any(r.must and r.outcome == "fail" for r in judged):
            outcome = "fail"
        elif any(r.must and r.outcome == "ungradeable" for r in judged):
            outcome = "ungradeable"
    cause = None
    if outcome == "ungradeable":
        cause = next((r.cause for r in results if r.must and r.outcome == "ungradeable"), None)
    return Verdict(outcome, tuple(results), cause)


def bundle_path(rec: EpisodeRecord) -> Path | None:
    """Where the bundle sits on disk, when it was written."""
    return rec.path
