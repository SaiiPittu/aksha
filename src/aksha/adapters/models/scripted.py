"""Scripted adapters: fixed replies in order, for unit tests (the ``scripted`` run mode).

Each reply is a result or an exception to raise. A script asked for one reply too many
raises ``PermanentModelError`` with code ``script_exhausted`` naming the step, so the run
fails loudly instead of inventing an answer. ``calls`` lists the step IDs asked, so a test
can assert what ran (a replayed step asks nothing).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar, Literal

from aksha.runtime.models.errors import PermanentModelError
from aksha.runtime.models.types import (
    FIELD_TYPES,
    QUESTION_TYPES,
    ClassificationRequest,
    ClassificationResult,
    ExtractionRequest,
    ExtractionResult,
    GenerationRequest,
    GenerationResult,
    ModelCapabilities,
)

SCRIPT_EXHAUSTED = "script_exhausted"


class _Script[T]:
    def __init__(self, replies: Iterable[T | BaseException]) -> None:
        self.replies: list[T | BaseException] = list(replies)
        self.calls: list[str] = []

    def next(self, step_id: str) -> T:
        self.calls.append(step_id)
        if not self.replies:
            raise PermanentModelError(SCRIPT_EXHAUSTED, f"no scripted reply left for {step_id}")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class ScriptedGenerationModel(_Script[GenerationResult]):
    """Generation replies in order."""

    model_class: ClassVar[Literal["generation"]] = "generation"
    capabilities: ClassVar[ModelCapabilities] = ModelCapabilities(native=True, offline=True)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """The next reply."""
        return self.next(request.step_id)


class ScriptedExtractionModel(_Script[ExtractionResult]):
    """Extraction replies in order; native, with spans, unless ``capabilities`` says not."""

    model_class: ClassVar[Literal["extraction"]] = "extraction"

    def __init__(
        self,
        replies: Iterable[ExtractionResult | BaseException],
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        """Script ``replies``; ``capabilities`` defaults to a native span-returning extractor."""
        super().__init__(replies)
        self.capabilities = capabilities or ModelCapabilities(
            native=True, field_types=FIELD_TYPES, returns_spans=True, offline=True
        )

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """The next reply."""
        return self.next(request.step_id)


class ScriptedClassificationModel(_Script[ClassificationResult]):
    """Classification replies in order; ``native=False`` scripts an adapted classifier."""

    model_class: ClassVar[Literal["classification"]] = "classification"

    def __init__(
        self, replies: Iterable[ClassificationResult | BaseException], *, native: bool = True
    ) -> None:
        """Script ``replies`` from a native (or adapted) classifier with a distribution."""
        super().__init__(replies)
        self.capabilities = ModelCapabilities(
            native=native, question_types=QUESTION_TYPES, returns_distribution=True, offline=True
        )

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """The next reply."""
        return self.next(request.step_id)
