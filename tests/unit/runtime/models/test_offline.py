"""``OfflineReaders`` judges an in-process rule reader by the declared thresholds."""

from __future__ import annotations

from decimal import Decimal

import pytest

from aksha.runtime.grounding.types import ReturnedText, SpanDraft
from aksha.runtime.models.fixed import ppm
from aksha.runtime.models.offline import OfflineReaders
from aksha.runtime.models.rules import RuleExtractionModel
from aksha.runtime.models.types import (
    ExtractionRequest,
    ExtractionResult,
    FieldAnswer,
    FieldSpec,
    ModelAlias,
    SpanRef,
)


class Title(RuleExtractionModel):
    def __init__(self, confidence: str) -> None:
        self.sure = Decimal(confidence)

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        s = request.state[0]
        answer = FieldAnswer(s.text, (SpanRef(s.span_id, 0, len(s.text)),), ppm(Decimal("0.01")),
                             ppm(self.sure))  # fmt: skip
        return ExtractionResult(answers=(("title", answer),))


ALIAS = ModelAlias("titles", "extraction", fields=(FieldSpec("title", "text"),))
TEXT = ReturnedText("inputs", "input/a.md", "v1", "A title\n\nbody")
PASSAGES = (SpanDraft("inputs", "input/a.md", "v1", 0, 7),)


def test_an_answer_over_the_threshold_is_accepted_with_its_span() -> None:
    (j,) = OfflineReaders({"titles": Title("0.95")}, (ALIAS,)).extract("titles", TEXT, PASSAGES)
    assert j.accepted and j.value == "A title" and j.spans == (SpanRef("p0", 0, 7),)


def test_an_answer_under_the_threshold_abstains() -> None:
    (j,) = OfflineReaders({"titles": Title("0.2")}, (ALIAS,)).extract("titles", TEXT, PASSAGES)
    assert not j.accepted and j.reason == "under_accept"


def test_an_alias_with_no_reader_is_an_error() -> None:
    with pytest.raises(KeyError):
        OfflineReaders({}, (ALIAS,)).extract("titles", TEXT, PASSAGES)
