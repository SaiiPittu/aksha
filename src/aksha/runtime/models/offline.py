"""Fast-class readers run in process, for a rule-based model reading what its request carries.

A rule-based generation model (``RuleGenerationModel``) stands in for a live model in the
``fake`` run mode. When a read tool hands it a document's text, it reads that text the way
the agent's own offline fast readers would: :class:`OfflineReaders` is the
``ToolModels`` handle over those rule adapters, judged by the same thresholds as a
recorded call (native), and recording nothing, because what it reads is already on the
log as the tool's result. It never reaches a model, the network or the store; a live model
replaces it by reading the text itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from aksha.runtime.grounding.types import CitationSpan, ReturnedText, SpanDraft
from aksha.runtime.models.calls import passage_id
from aksha.runtime.models.thresholds import threshold_classification, threshold_extraction
from aksha.runtime.models.types import (
    ClassificationRequest,
    ExtractionRequest,
    FieldSpec,
    Judgement,
    ModelAlias,
    QuestionSpec,
)


class OfflineReaders:
    """A ``ToolModels`` over rule adapters by alias (module doc)."""

    def __init__(self, adapters: Mapping[str, Any], aliases: Sequence[ModelAlias] = ()) -> None:
        """Answer each alias of ``adapters``; ``aliases`` give the default fields and questions."""
        self._adapters = dict(adapters)
        self._declared = {a.name: a for a in aliases}

    def _adapter(self, alias: str) -> Any:
        if alias not in self._adapters:
            raise KeyError(f"no offline reader for {alias!r}")
        return self._adapters[alias]

    def extract(
        self,
        alias: str,
        text: ReturnedText,
        passages: Sequence[SpanDraft],
        fields: Sequence[FieldSpec] | None = None,
    ) -> tuple[Judgement, ...]:
        """Fill ``fields`` (default: the alias's own) from ``passages`` of ``text``."""
        declared = self._declared.get(alias)
        asked = tuple(fields) if fields is not None else (declared.fields if declared else ())
        state = tuple(
            CitationSpan(
                passage_id(n),
                "",
                p.source,
                p.record_id,
                p.version,
                p.start,
                p.end,
                text.text[p.start : p.end],
                "offline",
            )
            for n, p in enumerate(passages)
        )
        result = self._adapter(alias).extract(
            ExtractionRequest(f"offline/{alias}", alias, state, asked)
        )
        by_id = {s.span_id: s for s in state}
        return tuple(threshold_extraction(result.answer(f.name), f, by_id, True) for f in asked)

    def classify(
        self,
        alias: str,
        state: str,
        questions: Sequence[QuestionSpec] | None = None,
        state_inputs: Sequence[str] = (),
    ) -> tuple[Judgement, ...]:
        """Answer ``questions`` (default: the alias's own) about ``state``."""
        declared = self._declared.get(alias)
        asked = (
            tuple(questions) if questions is not None else (declared.questions if declared else ())
        )
        request = ClassificationRequest(
            f"offline/{alias}", alias, state, asked, tuple(state_inputs)
        )
        result = self._adapter(alias).classify(request)
        return tuple(threshold_classification(result.answer(q.name), q, True) for q in asked)
