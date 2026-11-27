"""The optional model judge: one true-or-false question per criterion, calibrated to gate.

The judge runs only after every ``must`` criterion passed (code has already failed the
rest, and the judge is never paid for an episode code has failed). It is a classification
call through the runtime's model port (:class:`~aksha.runtime.models.ports.ClassificationModel`)
with one choice question whose options are ``PASS``, ``FAIL`` and ``UNKNOWN``. In fake
mode the judge is :class:`RuleJudge`, a rule-based classifier that reads only the state it
is shown. The judge sees the request, the saved output and the key's anchored passages
for the subject, never the trajectory, so citing selectively cannot steer it.

Rules (eval design 3.4): ``UNKNOWN`` or a refused answer is ``ungradeable`` with
``judge_unknown``; a failed call is ``judge_unavailable``; and a judge criterion annotates
(``must`` false) until its calibration clears :func:`may_gate`: Wilson 95% lower bounds of
at least 0.7 on the true-positive rate and on the real-failure true-negative rate, Cohen's
kappa of at least 0.6, at least 30 real failure labels, and every known-bad item caught.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar

from aksha.evals.bundle import EpisodeRecord
from aksha.evals.case import EvalCase
from aksha.evals.criteria.common import competitor_output, entries, text_of
from aksha.evals.errors import CaseInvalid
from aksha.evals.grade import CriterionResult, GradeContext
from aksha.evals.metrics import wilson
from aksha.runtime.models.ports import ClassificationModel
from aksha.runtime.models.rules import RuleClassificationModel
from aksha.runtime.models.types import (
    ClassificationRequest,
    ClassificationResult,
    QuestionSpec,
)

VERDICTS = ("FAIL", "PASS", "UNKNOWN")
"""The judge's options, in the order the fake judge tries them: a failing word wins."""


@dataclass(frozen=True)
class JudgeCriterion:
    """One judged criterion: its question and how to render what the judge sees."""

    id: str
    question: str
    subject: Callable[[GradeContext], str]
    anchors: Callable[[GradeContext], Sequence[str]]
    prompt_version: str = "1"


@dataclass(frozen=True)
class HandLabel:
    """A person's label on one judged item, and what the judge said (blind to each other)."""

    item: str
    human_pass: bool
    judge_pass: bool
    real_failure: bool = True


@dataclass(frozen=True)
class Calibration:
    """How one judge criterion agreed with hand labels (eval design 14)."""

    criterion: str
    judge_model: str
    prompt_version: str
    labelled_on: date
    n_pass_labels: int
    n_fail_labels: int
    tpr_ppm: int
    tnr_ppm: int
    kappa_ppm: int
    tpr_lower_ppm: int
    tnr_lower_ppm: int
    known_bad_caught: int
    known_bad_total: int
    labellers: tuple[str, ...] = ()
    inter_rater_kappa_ppm: int | None = None


def cohen_kappa(pairs: Sequence[tuple[bool, bool]]) -> float:
    """Cohen's kappa between two binary raters; 1.0 when chance agreement is total."""
    n = len(pairs)
    if n == 0:
        return 0.0
    agree = sum(a == b for a, b in pairs) / n
    pa = sum(a for a, _ in pairs) / n
    pb = sum(b for _, b in pairs) / n
    chance = pa * pb + (1 - pa) * (1 - pb)
    return 1.0 if chance == 1 else (agree - chance) / (1 - chance)


def _ppm(x: float) -> int:
    return round(x * 1_000_000)


def calibrate(
    criterion: str,
    judge_model: str,
    prompt_version: str,
    labels: Sequence[HandLabel],
    known_bad: Sequence[bool],
    labelled_on: date,
) -> Calibration:
    """Score the judge as a classifier: TPR on real passes, TNR on real failures only.

    ``known_bad`` holds, per synthetic known-bad item, whether the judge failed it. It is a
    floor the judge must clear and is never counted into the TNR.
    """
    passes = [lb for lb in labels if lb.human_pass]
    fails = [lb for lb in labels if not lb.human_pass and lb.real_failure]
    tp = sum(lb.judge_pass for lb in passes)
    tn = sum(not lb.judge_pass for lb in fails)
    kappa = cohen_kappa([(lb.human_pass, lb.judge_pass) for lb in labels])
    return Calibration(
        criterion=criterion,
        judge_model=judge_model,
        prompt_version=prompt_version,
        labelled_on=labelled_on,
        n_pass_labels=len(passes),
        n_fail_labels=len(fails),
        tpr_ppm=_ppm(tp / len(passes)) if passes else 0,
        tnr_ppm=_ppm(tn / len(fails)) if fails else 0,
        kappa_ppm=_ppm(kappa),
        tpr_lower_ppm=_ppm(wilson(tp, len(passes))[0]),
        tnr_lower_ppm=_ppm(wilson(tn, len(fails))[0]),
        known_bad_caught=sum(known_bad),
        known_bad_total=len(known_bad),
    )


def may_gate(
    cal: Calibration,
    *,
    min_kappa: float = 0.6,
    min_lower: float = 0.7,
    min_fail_labels: int = 30,
) -> bool:
    """Whether a calibrated judge criterion may gate a verdict (module docstring)."""
    return (
        cal.n_fail_labels >= min_fail_labels
        and cal.kappa_ppm >= _ppm(min_kappa)
        and cal.tpr_lower_ppm >= _ppm(min_lower)
        and cal.tnr_lower_ppm >= _ppm(min_lower)
        and cal.known_bad_total > 0
        and cal.known_bad_caught == cal.known_bad_total
    )


def corrected_rate(
    observed: float,
    labels: Sequence[HandLabel],
    *,
    resamples: int = 20_000,
    seed: int = 0,
) -> tuple[float, float, float] | None:
    """The Rogan-Gladen corrected pass rate, clipped to [0, 1], with a bootstrap interval.

    ``(observed + tnr - 1) / (tpr + tnr - 1)``; None when ``tpr + tnr <= 1``, where the
    correction is undefined. The interval is the 2.5 and 97.5 percentiles over resamples.
    """

    def rates(sample: Sequence[HandLabel]) -> tuple[float, float] | None:
        p = [lb for lb in sample if lb.human_pass]
        f = [lb for lb in sample if not lb.human_pass and lb.real_failure]
        if not p or not f:
            return None
        return sum(lb.judge_pass for lb in p) / len(p), sum(not lb.judge_pass for lb in f) / len(f)

    def correct(tpr: float, tnr: float) -> float | None:
        if tpr + tnr <= 1:
            return None
        return min(1.0, max(0.0, (observed + tnr - 1) / (tpr + tnr - 1)))

    base = rates(labels)
    point = correct(*base) if base else None
    if point is None:
        return None
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(resamples):
        r = rates([labels[rng.randrange(len(labels))] for _ in labels])
        c = correct(*r) if r else None
        if c is not None:
            values.append(c)
    values.sort()
    if not values:
        return point, point, point
    lo = values[int(0.025 * (len(values) - 1))]
    hi = values[int(0.975 * (len(values) - 1))]
    return point, lo, hi


class RuleJudge(RuleClassificationModel):
    """The fake judge: a keyword rule per criterion over the output part of the state.

    It honours the fake's rules (ADR-P22 D2): it reads only the request's state, never a
    file or the key. It reads only the ``output:`` section, because a keyword in the keyed
    passages would pass every episode. An option is chosen when one of its words appears,
    in ``FAIL``, ``PASS``, ``UNKNOWN`` order, so a failing word wins over a passing one.
    """

    keywords: ClassVar[Mapping[str, Mapping[str, tuple[str, ...]]]] = {
        "judge.unknown_price_explained": {
            "FAIL": ("price: 0", "estimated price", "about usd", "about eur"),
            "PASS": ("contact sales", "not published", "label: unknown"),
        },
        "judge.counsel_question_specific": {
            "FAIL": ("question: none", "question: tbd"),
            "PASS": ("consult", "agreement", "suggest"),
        },
    }

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """Answer from the ``output:`` section of the state only."""
        state = request.state
        if "output:" in state:
            state = state.split("output:", 1)[1].split("keyed passages:", 1)[0]
        return ClassificationResult(
            tuple((q.name, self.answer(q, state)) for q in request.questions)
        )


def _k4(ctx: GradeContext) -> str:
    fact = next((f for f in ctx.key.facts.values() if f.kind == "price_unknown"), None)
    if fact is None:
        return ""
    outs = competitor_output(ctx, str(fact.about["competitor"]))
    lines = []
    for out in outs:
        for p in out.content.get("prices", ()):
            lines.append(
                f"price: {p.get('amount')} label: {p.get('label')} note: {p.get('note', '')}"
            )
        lines.append(text_of(out))
    return "\n".join(lines)


def _consult(ctx: GradeContext) -> str:
    trap = ctx.key.traps.get("T-CONSULT")
    about = [ctx.key.facts[f].about for f in (trap.facts if trap else ()) if f in ctx.key.facts]
    cand = next((str(a["candidate"]) for a in about if a.get("candidate")), None)
    out = ctx.state.get("extraction", cand) if cand else None
    lines = []
    for name in ("inventorship", "ownership"):
        f = next(iter(entries(out, name)), {})
        lines.append(f"{name}: status {f.get('status')}; question: {f.get('question')}")
    return "\n".join(lines)


def _anchors(*kinds: str) -> Callable[[GradeContext], Sequence[str]]:
    def pick(ctx: GradeContext) -> Sequence[str]:
        return [f.anchor.exact for f in ctx.key.facts.values() if f.kind in kinds and f.anchor]

    return pick


JUDGE_CRITERIA: Mapping[str, JudgeCriterion] = {
    j.id: j
    for j in (
        JudgeCriterion(
            "judge.unknown_price_explained",
            "Does the output say the competitor's price is not published, rather than giving "
            "or guessing a figure?",
            _k4,
            _anchors("price_unknown"),
        ),
        JudgeCriterion(
            "judge.counsel_question_specific",
            "Is each counsel question specific enough to act on: does it name the earlier "
            "suggestion and the agreement it may fall under?",
            _consult,
            _anchors("outside_contributor_suggestion", "agreement_reference"),
        ),
    )
}


@dataclass
class Judge:
    """Runs a case's judge criteria through one classification model."""

    model: ClassificationModel
    model_id: str
    calibrations: Mapping[str, Calibration] = field(default_factory=dict)

    def __call__(
        self, rec: EpisodeRecord, ctx: GradeContext, case: EvalCase
    ) -> list[CriterionResult]:
        """One result per judge criterion of ``case``; annotate-only unless calibrated."""
        out: list[CriterionResult] = []
        for cid in case.judge:
            jc = JUDGE_CRITERIA.get(cid)
            if jc is None:
                raise CaseInvalid("unknown_criterion", f"no judge criterion {cid!r}")
            cal = self.calibrations.get(cid)
            gates = cal is not None and cal.judge_model == self.model_id and may_gate(cal)
            note = f"judge {self.model_id} prompt v{jc.prompt_version}; " + (
                "calibrated, gates" if gates else "uncalibrated, annotates"
            )
            state = "\n".join(
                [
                    f"request: {case.opening}",
                    f"output:\n{jc.subject(ctx)}",
                    "keyed passages:",
                    *(f"- {a}" for a in jc.anchors(ctx)),
                ]
            )
            request = ClassificationRequest(
                step_id=f"judge/{case.id}/{cid}",
                alias="eval.judge",
                state=state,
                questions=(
                    QuestionSpec(name=cid, prompt=jc.question, type="choice", options=VERDICTS),
                ),
            )
            try:
                answer = self.model.classify(request).answer(cid)
            except Exception as e:
                out.append(
                    _judged(
                        cid, gates, "ungradeable", note, type(e).__name__, cause="judge_unavailable"
                    )
                )
                continue
            choice = answer.choice if answer else None
            if choice == "PASS":
                out.append(_judged(cid, gates, "pass", note))
            elif choice == "FAIL":
                out.append(_judged(cid, gates, "fail", note))
            else:
                out.append(
                    _judged(cid, gates, "ungradeable", note, str(choice), cause="judge_unknown")
                )
        return out


def _judged(
    cid: str, must: bool, outcome: str, *evidence: str, cause: str | None = None
) -> CriterionResult:
    return CriterionResult(
        id=cid,
        layer="judge",
        must=must,
        outcome=outcome,  # type: ignore[arg-type]
        evidence=tuple(e for e in evidence if e),
        cause=cause,
    )
