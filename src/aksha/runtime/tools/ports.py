"""The fast-model handle a tool body is given (ADR-P07 D6 as amended G1).

A read tool that must understand what an input *holds* (which passages state a price,
which column holds the review text, which section is a mechanism) asks a fast-class model
through ``ToolContext.models`` instead of matching a layout in code. The handle runs the
alias's cascade in the body's attempt and records every answer and judgement on the tool
step, so a completed step replays them without asking again.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from aksha.runtime.grounding.types import ReturnedText, SpanDraft
    from aksha.runtime.models.types import FieldSpec, Judgement, QuestionSpec


class ToolModels(Protocol):
    """Extraction and classification for one tool call, recorded on its step."""

    def extract(
        self,
        alias: str,
        text: ReturnedText,
        passages: Sequence[SpanDraft],
        fields: Sequence[FieldSpec] | None = None,
    ) -> Sequence[Judgement]:
        """Fill ``fields`` (default: the alias's own) from ``passages`` of ``text``.

        Each judgement's spans are offsets inside the passage they name; ``draft`` turns
        one into a draft over the record, so a value can be cited.
        """
        ...

    def classify(
        self,
        alias: str,
        state: str,
        questions: Sequence[QuestionSpec] | None = None,
        state_inputs: Sequence[str] = (),
    ) -> Sequence[Judgement]:
        """Answer ``questions`` (default: the alias's own) about ``state``, rendered by code."""
        ...
