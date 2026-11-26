"""Decide, pick the schema, extract (ADR-P07 G3): what a record of unknown kind reads as.

The pipeline runs through ``ToolModelCalls`` over rule doubles, so every step is judged by
the real thresholds: an answer in the abstention band reads ``other``, a field the
extractor abstains on is left for the general model, and a value comes back as the record
range of the words it was read from. An adapted extractor (a general model whose quotes
code located) fills a cited field; one whose quote is nowhere in the passage does not.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar

from aksha.runtime.grounding.segment import segment
from aksha.runtime.grounding.types import ReturnedText, SpanDraft
from aksha.runtime.models.calls import ToolModelCalls
from aksha.runtime.models.documents import (
    OTHER_KIND,
    DocumentKind,
    document_preview,
    kind_question,
    read_document,
)
from aksha.runtime.models.fixed import distribution_ppm, ppm
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.rules import (
    RuleClassificationModel,
    RuleExtractionModel,
    RuleGenerationModel,
)
from aksha.runtime.models.types import (
    ClassAnswer,
    ClassificationRequest,
    ClassificationResult,
    ExtractionRequest,
    ExtractionResult,
    FieldAnswer,
    FieldSpec,
    ModelAlias,
    ModelCapabilities,
    SpanRef,
)

RAW = "Acme Widget by Acme Ltd\n\nThe widget costs EUR 9 per month.\n"
TEXT = ReturnedText("inputs", "input/a.txt", "v1", RAW)
KINDS = (
    DocumentKind("offer", "a seller's page", "offer_facts"),
    DocumentKind("note", "the person's own note"),
)
FIELDS = (FieldSpec("price", description="the price"), FieldSpec("maker", description="who"))


class Decide(RuleClassificationModel):
    def __init__(self, choice: str, sure: str) -> None:
        self.choice, self.sure = choice, Decimal(sure)

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        q = request.questions[0]
        rest = (1 - self.sure) / (len(q.options) - 1)
        weights = {o: self.sure if o == self.choice else rest for o in q.options}
        return ClassificationResult(
            ((q.name, ClassAnswer(self.choice, distribution_ppm(weights))),)
        )


class Extract(RuleExtractionModel):
    """Finds the price in passage p1 and abstains (low confidence) on the maker."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        text = request.state[1].text
        at = text.index("EUR 9 per month")
        price = FieldAnswer("EUR 9 per month", (SpanRef("p1", at, at + 15),),
                            ppm(Decimal("0.01")), ppm(Decimal("0.95")))  # fmt: skip
        maker = FieldAnswer("Acme Ltd", (SpanRef("p0", 15, 23),), ppm(Decimal("0.01")),
                            ppm(Decimal("0.4")))  # fmt: skip
        return ExtractionResult((("price", price), ("maker", maker)))


class Adapted(Extract):
    capabilities: ClassVar[ModelCapabilities] = ModelCapabilities(
        native=False, field_types=frozenset({"text"}), returns_spans=True
    )


def _calls(decide: Any, extract: Any, allow_adapted: bool = False) -> ToolModelCalls:
    aliases = (
        ModelAlias("doc_kind", "classification", on_exhausted="unknown"),
        ModelAlias("offer_facts", "extraction", fields=FIELDS, on_exhausted="unknown",
                   allow_adapted=allow_adapted),
    )  # fmt: skip
    table = bind_aliases(aliases, "fake", {"generate": RuleGenerationModel(), "doc_kind": decide,
                                           "offer_facts": extract})  # fmt: skip
    return ToolModelCalls(table, frozenset({"doc_kind", "offer_facts"}), "read", "o")


def _passages() -> list[SpanDraft]:
    return [SpanDraft("inputs", "input/a.txt", "v1", s.start, s.end) for s in segment(RAW, "text")]


def _state() -> str:
    return document_preview("input/a.txt", "text", RAW, segment(RAW, "text"))


def test_the_decide_question_lists_every_kind_with_its_meaning_and_other() -> None:
    q = kind_question(KINDS)
    assert q.options == ("offer", "note", OTHER_KIND)
    assert "- offer: a seller's page" in q.prompt and "- other:" in q.prompt


def test_a_decided_kind_is_extracted_by_its_schema_and_cited_by_record_range() -> None:
    got = read_document(_calls(Decide("offer", "0.95"), Extract()), "doc_kind", KINDS, TEXT,
                        _passages(), _state())  # fmt: skip
    assert (got.kind, got.decided) == ("offer", True)
    value, at = got.first("price")
    assert value == "EUR 9 per month" and at is not None
    assert RAW[at[0] : at[1]] == "EUR 9 per month"
    # the maker came back under its confidence threshold: left to the general model
    assert got.unresolved == ("maker",) and got.needs_reader


def test_an_unsure_decision_reads_other_and_extracts_nothing() -> None:
    calls = _calls(Decide("offer", "0.6"), Extract())
    got = read_document(calls, "doc_kind", KINDS, TEXT, _passages(), _state())
    assert (got.kind, got.decided, got.decision) == (OTHER_KIND, False, "under_accept")
    assert got.values == {} and got.unresolved == ("kind",)
    assert [c["alias"] for c in calls.calls] == ["doc_kind"]  # no schema was asked


def test_a_kind_with_no_schema_is_for_the_general_model_alone() -> None:
    got = read_document(_calls(Decide("note", "0.95"), Extract()), "doc_kind", KINDS, TEXT,
                        _passages(), _state())  # fmt: skip
    assert (got.kind, got.values, got.unresolved) == ("note", {}, ())


def test_an_adapted_extractor_fills_the_same_schema_when_allowed() -> None:
    got = read_document(_calls(Decide("offer", "0.95"), Adapted(), allow_adapted=True),
                        "doc_kind", KINDS, TEXT, _passages(), _state())  # fmt: skip
    assert got.values["price"] == "EUR 9 per month"


def test_the_preview_is_structure_only() -> None:
    state = _state()
    assert state.startswith("record: input/a.txt\nformat: text")
    assert "EUR 9 per month" in state  # the start of the text, as a reader sees it
