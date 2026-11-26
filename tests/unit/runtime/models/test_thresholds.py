"""Code applies every threshold; a model never does (ADR-P07 D4). Pure functions."""

from __future__ import annotations

import itertools

import pytest

from aksha.runtime.grounding.types import CitationSpan
from aksha.runtime.models.thresholds import (
    threshold_classification,
    threshold_extraction,
    unfitted_thresholds,
)
from aksha.runtime.models.types import (
    ClassAnswer,
    ConfidenceThresholds,
    FieldAnswer,
    FieldSpec,
    ModelAlias,
    QuestionSpec,
    SpanRef,
)

PAGE = "Pricing: 279 EUR per sensor, billed yearly."
SPAN = CitationSpan("EX-0001", "o", "fixtures", "pricing/acme", "v1", 0, len(PAGE), PAGE, "read")
STATE = {SPAN.span_id: SPAN}
PRICE = FieldSpec("amount", "number")
WHERE = SpanRef("EX-0001", 9, 12)  # "279"

SUPPORT = QuestionSpec(
    "support",
    "Does claim 1 disclose feature F2?",
    options=("yes", "partial", "no"),
    thresholds=ConfidenceThresholds(accept_at=0.9),
    adapted=ConfidenceThresholds(accept_at=0.85),
)
FLAKY = QuestionSpec(
    "flaky", "Is this failure flaky?", type="boolean",
    thresholds=ConfidenceThresholds(accept_at=0.8, reject_at=0.2),
)  # fmt: skip


def dist(**ppm: int) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(ppm.items(), key=lambda kv: (-kv[1], kv[0])))


# --- extraction ------------------------------------------------------------------------


def test_a_confident_cited_value_inside_the_state_is_accepted() -> None:
    answer = FieldAnswer("279", (WHERE,), p_null_ppm=10_000, confidence_ppm=950_000)
    j = threshold_extraction(answer, PRICE, STATE, native=True)
    assert (j.outcome, j.value, j.reason, j.spans) == (
        "accept",
        "279",
        "at_or_over_accept",
        (WHERE,),
    )


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (FieldAnswer("279", (WHERE,), confidence_ppm=899_999), "under_accept"),
        (FieldAnswer("279", (), confidence_ppm=990_000), "span_missing"),
        (FieldAnswer("279", (SpanRef("EX-0001", 40, 99),), confidence_ppm=990_000), "span_outside"),
        (FieldAnswer("279", (SpanRef("EX-0009", 0, 3),), confidence_ppm=990_000), "span_outside"),
        (FieldAnswer("279", (SpanRef("EX-0001", 5, 5),), confidence_ppm=990_000), "span_outside"),
        (FieldAnswer("279 EUR", (WHERE,), confidence_ppm=990_000), "type_invalid"),
        (FieldAnswer("279", (WHERE,), confidence_ppm=None), "confidence_missing"),
        (FieldAnswer(None, (), p_null_ppm=499_999), "null_unsure"),
        (FieldAnswer(None, (), p_null_ppm=None), "null_unsure"),
        (FieldAnswer("279", (WHERE,), confidence_ppm=1_000_001), "probability_invalid"),
    ],
)
def test_every_other_extraction_answer_abstains(answer: FieldAnswer, reason: str) -> None:
    j = threshold_extraction(answer, PRICE, STATE, native=True)
    assert (j.outcome, j.reason) == ("abstain", reason)


def test_a_missing_value_is_absent_only_at_or_over_null_at() -> None:
    j = threshold_extraction(FieldAnswer(None, (), p_null_ppm=500_000), PRICE, STATE, native=True)
    assert (j.outcome, j.value, j.reason) == ("accept", None, "absent")


def test_a_field_the_adapter_left_out_abstains() -> None:
    j = threshold_extraction(None, PRICE, STATE, native=True)
    assert (j.outcome, j.reason) == ("abstain", "missing_answer")


def test_an_uncited_field_needs_no_span_but_an_adapted_answer_never_fills_a_cited_one() -> None:
    loose = FieldSpec("note", "text", cited=False)
    j = threshold_extraction(
        FieldAnswer("ok", (), confidence_ppm=950_000), loose, STATE, native=False
    )
    assert j.accepted
    cited = FieldAnswer("279", (WHERE,), confidence_ppm=990_000)
    j = threshold_extraction(cited, PRICE, STATE, native=False)
    assert (j.outcome, j.reason) == ("abstain", "adapted_no_span")


@pytest.mark.parametrize(
    ("type_", "value", "ok"),
    [
        ("number", "12.50", True), ("number", "-3", True), ("number", "1e3", False),
        ("date", "2026-11-27", True), ("date", "2026-13-01", False), ("date", "27/11/2026", False),
        ("duration", "P1Y", True), ("duration", "P3M", True), ("duration", "PT12H", True),
        ("duration", "P", False), ("duration", "3 months", False),
        ("list", ("a", "b"), True), ("list", "a", False), ("text", ("a",), False),
    ],
)  # fmt: skip
def test_field_types_are_checked(type_: str, value: object, ok: bool) -> None:
    spec = FieldSpec("f", type_, cited=False)  # type: ignore[arg-type]
    answer = FieldAnswer(value, (), confidence_ppm=990_000)  # type: ignore[arg-type]
    j = threshold_extraction(answer, spec, STATE, native=True)
    assert j.accepted is ok
    if not ok:
        assert j.reason == "type_invalid"


# --- classification --------------------------------------------------------------------


def test_the_worked_example_native_abstains_and_adapted_accepts() -> None:
    """ADR-P07's worked example: native at 0.40 abstains; adapted at 0.90 accepts."""
    native = ClassAnswer("partial", dist(partial=400_000, yes=350_000, no=250_000))
    j = threshold_classification(native, SUPPORT, native=True)
    assert (j.outcome, j.reason) == ("abstain", "under_accept")
    adapted = ClassAnswer("partial", dist(partial=900_000, yes=60_000, no=40_000))
    j = threshold_classification(adapted, SUPPORT, native=False)
    assert (j.outcome, j.value) == ("accept", "partial")
    # judged by the native thresholds, the same adapted answer would abstain at 0.9
    assert threshold_classification(adapted, SUPPORT, native=True).accepted
    lower = ClassAnswer("partial", dist(partial=600_000, yes=300_000, no=100_000))
    assert threshold_classification(lower, SUPPORT, native=False).reason == "under_accept"


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (ClassAnswer(None, ()), "refused"),
        (ClassAnswer("maybe", dist(maybe=1_000_000)), "not_an_option"),
        (ClassAnswer("yes", dist(yes=900_000, no=50_000)), "distribution_invalid"),
        (ClassAnswer("yes", dist(yes=950_000, no=50_000)), "distribution_invalid"),
        (ClassAnswer("no", dist(yes=950_000, partial=30_000, no=20_000)), "choice_not_top"),
        (ClassAnswer(True, dist(yes=1_000_000, partial=0, no=0)), "not_an_option"),
    ],
)
def test_every_malformed_classification_answer_abstains(answer: ClassAnswer, reason: str) -> None:
    j = threshold_classification(answer, SUPPORT, native=True)
    assert (j.outcome, j.reason) == ("abstain", reason)


def test_a_margin_needs_a_gap_between_the_top_two() -> None:
    spec = QuestionSpec("t", "?", options=("a", "b"),
                        thresholds=ConfidenceThresholds(accept_at=0.5, margin=0.3))  # fmt: skip
    close = ClassAnswer("a", dist(a=600_000, b=400_000))
    assert threshold_classification(close, spec, native=True).reason == "under_margin"
    wide = ClassAnswer("a", dist(a=700_000, b=300_000))
    assert threshold_classification(wide, spec, native=True).accepted


def test_a_boolean_has_an_abstention_band_between_reject_and_accept() -> None:
    def judge(p_true: int) -> tuple[str, object, str]:
        answer = ClassAnswer(p_true >= 500_000, dist(true=p_true, false=1_000_000 - p_true))
        j = threshold_classification(answer, FLAKY, native=True)
        return j.outcome, j.value, j.reason

    assert judge(800_000) == ("accept", True, "at_or_over_accept")
    assert judge(200_000) == ("accept", False, "at_or_under_reject")
    assert judge(799_999) == ("abstain", True, "abstention_band")
    assert judge(200_001) == ("abstain", False, "abstention_band")


def test_a_boolean_answer_must_be_a_boolean() -> None:
    answer = ClassAnswer("true", dist(true=900_000, false=100_000))
    assert threshold_classification(answer, FLAKY, native=True).reason == "not_an_option"


def test_every_answer_lands_in_exactly_one_outcome() -> None:
    """Property: over a grid of answers, accept or abstain, and accept implies the rules."""
    for top, choice in itertools.product(range(0, 1_000_001, 50_000), ("yes", "partial", None)):
        rest = 1_000_000 - top
        answer = ClassAnswer(choice, dist(yes=top, partial=rest // 2, no=rest - rest // 2))
        for native in (True, False):
            j = threshold_classification(answer, SUPPORT, native=native)
            assert j.outcome in ("accept", "abstain")
            if j.accepted:
                limit = 900_000 if native else 850_000
                assert choice == "yes" and top >= limit
    for conf, p_null, start in itertools.product((None, 0, 899_999, 900_000), (None, 0, 600_000),
                                                 (0, 9, 40)):  # fmt: skip
        field = FieldAnswer("279", (SpanRef("EX-0001", start, start + 3),), p_null, conf)
        j = threshold_extraction(field, PRICE, STATE, native=True)
        if j.accepted:
            assert j.spans and all(0 <= s.start < s.end <= len(PAGE) for s in j.spans)


def test_unfitted_thresholds_are_listed() -> None:
    fitted = ConfidenceThresholds(fitted_on="sha256:" + "a" * 64)
    aliases = (
        ModelAlias(
            "extract.price", "extraction", fields=(PRICE, FieldSpec("cur", thresholds=fitted))
        ),
        ModelAlias("classify.support", "classification", questions=(SUPPORT,)),
        ModelAlias("generate", "generation"),
    )
    assert unfitted_thresholds(aliases) == (
        "classify.support/support",
        "classify.support/support (adapted)",
        "extract.price/amount",
    )
