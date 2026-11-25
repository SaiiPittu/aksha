"""Results and judgements as recorded JSON values, and back (ADR-P07 D4, D7).

A model step records its adapter's result on ``step.completed``; a resumed run and a
replayed one read it back through these functions, so the recorded value is the answer.
Every probability is already an integer in parts per million and every ``number`` value a
decimal string, so the recorded bytes hold no float.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from aksha.runtime.canonical import thaw
from aksha.runtime.models.types import (
    ClassAnswer,
    ClassificationResult,
    ExtractionResult,
    FieldAnswer,
    FinishReason,
    GenerationResult,
    Judgement,
    Outcome,
    SpanRef,
    ToolCall,
    Usage,
)


def _spans_json(spans: tuple[SpanRef, ...]) -> list[dict[str, Any]]:
    return [{"span_id": s.span_id, "start": s.start, "end": s.end} for s in spans]


def _spans(value: object) -> tuple[SpanRef, ...]:
    items = cast(list[Mapping[str, Any]], value or [])
    return tuple(SpanRef(str(s["span_id"]), int(s["start"]), int(s["end"])) for s in items)


def _value_json(value: object) -> object:
    return list(value) if isinstance(value, tuple | list) else value


def _value(value: object) -> Any:
    return tuple(cast(list[str], value)) if isinstance(value, list | tuple) else value


def generation_result_json(result: GenerationResult) -> dict[str, Any]:
    """A generation result as JSON data."""
    return {
        "text": result.text,
        "tool_calls": [c.as_json() for c in result.tool_calls],
        "finish": result.finish,
        "usage": result.usage.as_json(),
    }


def generation_result(value: Mapping[str, Any]) -> GenerationResult:
    """The inverse of :func:`generation_result_json`."""
    calls = tuple(
        ToolCall(str(c["call_id"]), str(c["name"]), thaw(c.get("arguments", {})))
        for c in value.get("tool_calls", ())
    )
    usage = Usage(**{k: int(v) for k, v in dict(value.get("usage", {})).items()})
    text = value.get("text")
    finish = cast(FinishReason, value.get("finish", "text"))
    return GenerationResult(None if text is None else str(text), calls, finish, usage)


def extraction_result_json(result: ExtractionResult) -> list[dict[str, Any]]:
    """An extraction result as JSON data, one entry per field."""
    return [
        {
            "name": name,
            "value": _value_json(a.value),
            "spans": _spans_json(a.spans),
            "p_null_ppm": a.p_null_ppm,
            "confidence_ppm": a.confidence_ppm,
        }
        for name, a in result.answers
    ]


def extraction_result(value: object) -> ExtractionResult:
    """The inverse of :func:`extraction_result_json`."""
    items = cast(list[Mapping[str, Any]], value)
    return ExtractionResult(
        tuple(
            (
                str(i["name"]),
                FieldAnswer(
                    _value(i.get("value")),
                    _spans(i.get("spans")),
                    i.get("p_null_ppm"),
                    i.get("confidence_ppm"),
                ),
            )
            for i in items
        )
    )


def classification_result_json(result: ClassificationResult) -> list[dict[str, Any]]:
    """A classification result as JSON data, one entry per question."""
    return [
        {
            "name": name,
            "choice": a.choice,
            "probabilities_ppm": [[k, p] for k, p in a.probabilities_ppm],
        }
        for name, a in result.answers
    ]


def classification_result(value: object) -> ClassificationResult:
    """The inverse of :func:`classification_result_json`."""
    items = cast(list[Mapping[str, Any]], value)
    return ClassificationResult(
        tuple(
            (
                str(i["name"]),
                ClassAnswer(
                    i.get("choice"),
                    tuple((str(k), int(p)) for k, p in i.get("probabilities_ppm", ())),
                ),
            )
            for i in items
        )
    )


def judgement_json(j: Judgement) -> dict[str, Any]:
    """A judgement as JSON data (``step`` is the step that records it, so it is left out)."""
    return {
        "name": j.name,
        "outcome": j.outcome,
        "value": _value_json(j.value),
        "reason": j.reason,
        "spans": _spans_json(j.spans),
    }


def judgement(value: Mapping[str, Any], step: str | None = None) -> Judgement:
    """The inverse of :func:`judgement_json`, with the step that recorded it."""
    return Judgement(
        str(value["name"]),
        cast(Outcome, value["outcome"]),
        _value(value.get("value")),
        str(value["reason"]),
        _spans(value.get("spans")),
        step,
    )
