"""Confidence thresholds and the abstention band, applied by code (ADR-P07 D4). Pure.

A fast-class answer is accepted only at or above ``accept_at`` (and, for a cited
extraction field, only with a span inside the minted citation spans it was given);
otherwise it abstains and the reason says why. A boolean question is accepted False at or
under ``reject_at``; between the two lies the abstention band. Every comparison is on
integers in parts per million, so two runs judge the same answer the same way.

Reasons. Accept: ``at_or_over_accept``, ``at_or_under_reject`` (a boolean False),
``absent`` (an extraction field at or over ``null_at``). Abstain: ``missing_answer``,
``probability_invalid``, ``refused``, ``not_an_option``, ``distribution_invalid``,
``choice_not_top``, ``under_accept``, ``under_margin``, ``abstention_band``,
``type_invalid``, ``adapted_no_span``, ``span_missing``, ``span_outside``,
``confidence_missing``, ``null_unsure``, ``null_conflict``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date

from aksha.runtime.grounding.types import CitationSpan
from aksha.runtime.models.fixed import ppm, top_two
from aksha.runtime.models.types import (
    FALSE,
    PPM,
    TRUE,
    ClassAnswer,
    FieldAnswer,
    FieldSpec,
    Judgement,
    ModelAlias,
    QuestionSpec,
    SpanRef,
)

_NUMBER = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_DURATION = re.compile(
    r"^P(?!$)([0-9]+Y)?([0-9]+M)?([0-9]+W)?([0-9]+D)?(T(?=[0-9])([0-9]+H)?([0-9]+M)?([0-9]+S)?)?$"
)


def _in_range(*values: int | None) -> bool:
    return all(v is None or (isinstance(v, int) and 0 <= v <= PPM) for v in values)


def _is_date(value: str) -> bool:
    if not _DATE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def type_ok(spec: FieldSpec, value: object) -> bool:
    """Whether ``value`` has the form ``spec.type`` declares."""
    if spec.type == "list":
        return isinstance(value, tuple | list) and all(isinstance(v, str) for v in value)
    if not isinstance(value, str):
        return False
    if spec.type == "number":
        return bool(_NUMBER.match(value))
    if spec.type == "date":
        return _is_date(value)
    if spec.type == "duration":
        return bool(_DURATION.match(value))
    return True


def _span_inside(ref: SpanRef, state: Mapping[str, CitationSpan]) -> bool:
    span = state.get(ref.span_id)
    return span is not None and 0 <= ref.start < ref.end <= len(span.text)


def threshold_extraction(
    answer: FieldAnswer | None,
    spec: FieldSpec,
    state: Mapping[str, CitationSpan],
    native: bool,
) -> Judgement:
    """Accept or abstain on one field's answer; ``state`` is the spans it was given, by ID."""
    name = spec.name

    def abstain(reason: str, value: str | tuple[str, ...] | None = None) -> Judgement:
        return Judgement(name, "abstain", value, reason)

    if answer is None:
        return abstain("missing_answer")
    if not _in_range(answer.p_null_ppm, answer.confidence_ppm):
        return abstain("probability_invalid", answer.value)
    t = spec.thresholds
    if answer.value is None:
        if answer.p_null_ppm is not None and answer.p_null_ppm >= ppm(t.null_at):
            return Judgement(name, "accept", None, "absent")
        return abstain("null_unsure")
    value = answer.value
    if not type_ok(spec, value):
        return abstain("type_invalid", value)
    if answer.p_null_ppm is not None and answer.p_null_ppm >= ppm(t.null_at):
        return abstain("null_conflict", value)
    if spec.cited:
        if not native:
            return abstain("adapted_no_span", value)
        if not answer.spans:
            return abstain("span_missing", value)
        if not all(_span_inside(ref, state) for ref in answer.spans):
            return abstain("span_outside", value)
    if answer.confidence_ppm is None:
        return abstain("confidence_missing", value)
    if answer.confidence_ppm < ppm(t.accept_at):
        return abstain("under_accept", value)
    return Judgement(name, "accept", value, "at_or_over_accept", answer.spans)


def _distribution_ok(answer: ClassAnswer, options: Iterable[str]) -> bool:
    probs = answer.probabilities_ppm
    keys = [k for k, _ in probs]
    return (
        len(keys) == len(set(keys))
        and set(keys) == set(options)
        and _in_range(*(p for _, p in probs))
        and sum(p for _, p in probs) == PPM
    )


def threshold_classification(
    answer: ClassAnswer | None, spec: QuestionSpec, native: bool
) -> Judgement:
    """Accept or abstain on one question's answer, by the native or the adapted thresholds."""
    name = spec.name
    if answer is None:
        return Judgement(name, "abstain", None, "missing_answer")
    choice = answer.choice
    if choice is None:
        return Judgement(name, "abstain", None, "refused")
    t = spec.thresholds_for(native)
    if spec.type == "boolean":
        if not isinstance(choice, bool):
            return Judgement(name, "abstain", None, "not_an_option")
        if not _distribution_ok(answer, (TRUE, FALSE)):
            return Judgement(name, "abstain", choice, "distribution_invalid")
        p_true = dict(answer.probabilities_ppm)[TRUE]
        if choice and p_true >= ppm(t.accept_at):
            return Judgement(name, "accept", True, "at_or_over_accept")
        if not choice and p_true <= ppm(t.reject_at):
            return Judgement(name, "accept", False, "at_or_under_reject")
        if (p_true >= ppm(t.accept_at)) or (p_true <= ppm(t.reject_at)):
            return Judgement(name, "abstain", choice, "choice_not_top")
        return Judgement(name, "abstain", choice, "abstention_band")
    if not isinstance(choice, str) or choice not in spec.options:
        return Judgement(name, "abstain", None, "not_an_option")
    if not _distribution_ok(answer, spec.options):
        return Judgement(name, "abstain", choice, "distribution_invalid")
    top = top_two(answer.probabilities_ppm)
    assert top is not None
    (_, best), runner_up = top
    mine = dict(answer.probabilities_ppm)[choice]
    if mine < best:
        return Judgement(name, "abstain", choice, "choice_not_top")
    if mine < ppm(t.accept_at):
        return Judgement(name, "abstain", choice, "under_accept")
    if t.margin is not None and mine - runner_up < ppm(t.margin):
        return Judgement(name, "abstain", choice, "under_margin")
    return Judgement(name, "accept", choice, "at_or_over_accept")


def unfitted_thresholds(aliases: Iterable[ModelAlias]) -> tuple[str, ...]:
    """Every threshold no labelled set has fitted, as ``alias/field`` or ``alias/question``.

    A question's adapted thresholds are listed as ``alias/question (adapted)``. Sorted.
    """
    out: list[str] = []
    for alias in aliases:
        for f in alias.fields:
            if f.thresholds.fitted_on is None:
                out.append(f"{alias.name}/{f.name}")
        for q in alias.questions:
            if q.thresholds.fitted_on is None:
                out.append(f"{alias.name}/{q.name}")
            if q.adapted is not None and q.adapted.fitted_on is None:
                out.append(f"{alias.name}/{q.name} (adapted)")
    return tuple(sorted(out))
