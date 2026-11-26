"""Rule-based adapters, the ``fake`` run mode's default: bases an agent's ``fakes.py`` extends.

A fake adapter answers from rules over what it is given, never from a network, so a run
in ``fake`` mode is offline and writes the same bytes twice (ADR-P07, adapters table):

1. ``RuleGenerationModel``: the agent writes ``generate``, a policy reading only the tool
   results in its context.
2. ``RuleExtractionModel``: ``patterns`` maps a field to a regular expression; the first
   match in the state is the value (group 1 when the pattern has a group), with its span.
   No match is "absent" with ``p_null`` of ``absent``. A field with no pattern is refused
   (no value, no ``p_null``), so it abstains.
3. ``RuleClassificationModel``: ``keywords`` maps a question to an option to words; the
   first option with a word in the state (case-folded substring) is the choice, with the
   declared constant ``confidence`` and the rest spread evenly. A boolean question reads
   the ``"true"`` words: P(true) is ``confidence`` on a hit and ``1 - confidence`` without.
   A choice question with no hit is refused.

Subclasses may override ``answer`` for anything a table cannot say.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from typing import ClassVar, Literal

from aksha.runtime.grounding.types import CitationSpan
from aksha.runtime.models.fixed import decimal_string, distribution_ppm, ppm
from aksha.runtime.models.types import (
    FALSE,
    FIELD_TYPES,
    QUESTION_TYPES,
    TRUE,
    ClassAnswer,
    ClassificationRequest,
    ClassificationResult,
    ExtractionRequest,
    ExtractionResult,
    FieldAnswer,
    FieldSpec,
    GenerationRequest,
    GenerationResult,
    ModelCapabilities,
    QuestionSpec,
    SpanRef,
)


class RuleGenerationModel:
    """A rule-based generation policy; subclasses implement :meth:`generate`."""

    model_class: ClassVar[Literal["generation"]] = "generation"
    capabilities: ClassVar[ModelCapabilities] = ModelCapabilities(native=True, offline=True)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """The policy's reply to ``request``; it reads only what the request carries."""
        raise NotImplementedError(f"{type(self).__name__} implements generate")


def _normalise(spec: FieldSpec, raw: str) -> str:
    if spec.type != "number":
        return raw
    try:
        return decimal_string(raw.replace(",", ""))
    except ValueError:
        return raw  # left as found: the type check makes it abstain


class RuleExtractionModel:
    """Extraction by regular expressions over the minted citation spans."""

    model_class: ClassVar[Literal["extraction"]] = "extraction"
    capabilities: ClassVar[ModelCapabilities] = ModelCapabilities(
        native=True, field_types=FIELD_TYPES, returns_spans=True, offline=True
    )
    patterns: ClassVar[Mapping[str, str]] = {}
    confidence: ClassVar[float] = 0.95
    absent: ClassVar[float] = 0.9

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """One answer per field, from :meth:`answer`."""
        return ExtractionResult(
            tuple((f.name, self.answer(f, request.state)) for f in request.fields)
        )

    def answer(self, spec: FieldSpec, state: tuple[CitationSpan, ...]) -> FieldAnswer:
        """The rule's answer to one field (module doc, item 2)."""
        pattern = self.patterns.get(spec.name)
        if pattern is None:
            return FieldAnswer(None)
        sure = Decimal(repr(self.confidence))
        found: list[tuple[str, SpanRef]] = []
        for span in state:
            for m in re.finditer(pattern, span.text):
                group = 1 if m.groups() else 0
                start, end = m.span(group)
                if end > start:
                    found.append((m.group(group), SpanRef(span.span_id, start, end)))
                if spec.type != "list" and found:
                    break
            if spec.type != "list" and found:
                break
        if not found:
            return FieldAnswer(None, (), p_null_ppm=ppm(self.absent))
        if spec.type == "list":
            value: str | tuple[str, ...] = tuple(raw for raw, _ in found)
            spans = tuple(ref for _, ref in found)
        else:
            raw, ref = found[0]
            value, spans = _normalise(spec, raw), (ref,)
        return FieldAnswer(value, spans, p_null_ppm=ppm(1 - sure), confidence_ppm=ppm(sure))


class RuleClassificationModel:
    """Classification by keywords, with a declared constant distribution."""

    model_class: ClassVar[Literal["classification"]] = "classification"
    capabilities: ClassVar[ModelCapabilities] = ModelCapabilities(
        native=True, question_types=QUESTION_TYPES, returns_distribution=True, offline=True
    )
    keywords: ClassVar[Mapping[str, Mapping[str, tuple[str, ...]]]] = {}
    confidence: ClassVar[float] = 0.9

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """One answer per question, from :meth:`answer`."""
        return ClassificationResult(
            tuple((q.name, self.answer(q, request.state)) for q in request.questions)
        )

    def answer(self, spec: QuestionSpec, state: str) -> ClassAnswer:
        """The rule's answer to one question (module doc, item 3)."""
        words = self.keywords.get(spec.name, {})
        text = state.casefold()
        sure = Decimal(repr(self.confidence))

        def hit(option: str) -> bool:
            return any(w.casefold() in text for w in words.get(option, ()))

        if spec.type == "boolean":
            p_true = sure if hit(TRUE) else 1 - sure
            return ClassAnswer(hit(TRUE), distribution_ppm({TRUE: p_true, FALSE: 1 - p_true}))
        chosen = next((o for o in spec.options if hit(o)), None)
        if chosen is None:
            return ClassAnswer(None)
        rest = (1 - sure) / (len(spec.options) - 1)
        weights = {o: sure if o == chosen else rest for o in spec.options}
        return ClassAnswer(chosen, distribution_ppm(weights))
