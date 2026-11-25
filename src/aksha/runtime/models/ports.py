"""The model interface: one Protocol per model class (ADR-P07 D1).

Each adapter translates between a provider and these owned request and result types, so
no provider's type enters the runtime or the event store. ``model_class`` and
``capabilities`` may be class attributes; routing reads them, never an adapter's name.
Every operation may raise ``TransientModelError`` (retried), ``PermanentModelError`` or
``ContextOverflow`` (:mod:`aksha.runtime.models.errors`).
"""

from __future__ import annotations

from typing import Literal, Protocol

from aksha.runtime.models.types import (
    ClassificationRequest,
    ClassificationResult,
    ExtractionRequest,
    ExtractionResult,
    GenerationRequest,
    GenerationResult,
    ModelCapabilities,
)


class GenerationModel(Protocol):
    """The slow class (System 2): messages and tools in, text and tool calls out."""

    @property
    def model_class(self) -> Literal["generation"]:
        """Always ``"generation"``."""
        ...

    @property
    def capabilities(self) -> ModelCapabilities:
        """The adapter's capability record."""
        ...

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Answer one generation request."""
        ...


class ExtractionModel(Protocol):
    """A fast class (System 1): fields in, typed values with spans or "absent" out."""

    @property
    def model_class(self) -> Literal["extraction"]:
        """Always ``"extraction"``."""
        ...

    @property
    def capabilities(self) -> ModelCapabilities:
        """The adapter's capability record."""
        ...

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """Answer every field of one extraction request."""
        ...


class ClassificationModel(Protocol):
    """A fast class (System 1): questions in, a listed option with its distribution out."""

    @property
    def model_class(self) -> Literal["classification"]:
        """Always ``"classification"``."""
        ...

    @property
    def capabilities(self) -> ModelCapabilities:
        """The adapter's capability record."""
        ...

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """Answer every question of one classification request."""
        ...


Adapter = GenerationModel | ExtractionModel | ClassificationModel
"""Any model adapter."""
