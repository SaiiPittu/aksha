"""An adapted classifier: a generation model under a forced-choice schema (ADR-P07 D5).

``AdaptedClassificationModel`` asks its generation model to call one tool, ``answer``,
whose schema lists each question's options (or a boolean) and a probability per option.
It is recorded ``native=False`` and judged by the question's adapted thresholds. Routing
lets it stand in only where the alias sets ``allow_adapted`` and gates no external
effect, and never for extraction, because it locates no span.

The model's probabilities arrive as JSON numbers; they are converted to parts per
million here, at the adapter's edge (D4). An answer missing, outside the options or
without a usable distribution is a refusal (``choice`` None), which abstains.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from aksha.runtime.models.fixed import distribution_ppm
from aksha.runtime.models.ports import GenerationModel
from aksha.runtime.models.types import (
    FALSE,
    QUESTION_TYPES,
    TRUE,
    ClassAnswer,
    ClassificationRequest,
    ClassificationResult,
    GenerationRequest,
    Message,
    ModelCapabilities,
    QuestionSpec,
)
from aksha.runtime.tools.types import ToolView

ANSWER_TOOL = "answer"

INSTRUCTION = (
    "Answer every question about the state below by calling the answer tool once. For each "
    "question give your choice and a probability from 0 to 1 for every option; the "
    "probabilities sum to 1. Do not guess: spread the probability when unsure."
)


def _options(q: QuestionSpec) -> tuple[str, ...]:
    return (TRUE, FALSE) if q.type == "boolean" else q.options


def answer_schema(request: ClassificationRequest) -> dict[str, Any]:
    """The ``answer`` tool's JSON Schema for ``request``'s questions."""
    props: dict[str, Any] = {}
    for q in request.questions:
        options = _options(q)
        choice: dict[str, Any] = (
            {"type": "boolean"} if q.type == "boolean" else {"enum": list(options)}
        )
        props[q.name] = {
            "type": "object",
            "properties": {
                "choice": choice,
                "probabilities": {
                    "type": "object",
                    "properties": {
                        o: {"type": "number", "minimum": 0, "maximum": 1} for o in options
                    },
                    "required": list(options),
                    "additionalProperties": False,
                },
            },
            "required": ["choice", "probabilities"],
        }
    return {
        "type": "object",
        "properties": {
            "answers": {
                "type": "object",
                "properties": props,
                "required": [q.name for q in request.questions],
            }
        },
        "required": ["answers"],
    }


def render(request: ClassificationRequest) -> str:
    """The user message: the state, then each question with its options."""
    lines = ["State:", request.state, "", "Questions:"]
    for q in request.questions:
        opts = "true or false" if q.type == "boolean" else ", ".join(q.options)
        lines.append(f"- {q.name}: {q.prompt} ({opts})")
    return "\n".join(lines)


def _answer(q: QuestionSpec, raw: object) -> ClassAnswer:
    if not isinstance(raw, Mapping):
        return ClassAnswer(None)
    choice, probs = raw.get("choice"), raw.get("probabilities")
    options = _options(q)
    if not isinstance(probs, Mapping) or set(probs) != set(options):
        return ClassAnswer(None)
    if q.type == "boolean" and not isinstance(choice, bool):
        return ClassAnswer(None)
    if q.type != "boolean" and choice not in options:
        return ClassAnswer(None)
    try:
        dist = distribution_ppm({str(k): v for k, v in probs.items()})
    except ValueError:
        return ClassAnswer(None)
    return ClassAnswer(choice if isinstance(choice, bool) else str(choice), dist)


class AdaptedClassificationModel:
    """A classifier made from a generation model; ``native=False`` (module doc)."""

    model_class: Literal["classification"] = "classification"

    def __init__(self, generation: GenerationModel) -> None:
        """Ask ``generation`` every question through the forced ``answer`` tool."""
        self._generation = generation

    @property
    def capabilities(self) -> ModelCapabilities:
        """Adapted, with a distribution, offline exactly when the generation model is."""
        return ModelCapabilities(
            native=False,
            question_types=QUESTION_TYPES,
            returns_distribution=True,
            offline=self._generation.capabilities.offline,
        )

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """One generation call; each question's answer read from the ``answer`` call."""
        asked = GenerationRequest(
            step_id=request.step_id,
            alias=request.alias,
            messages=(Message("system", INSTRUCTION), Message("user", render(request))),
            tools=(ToolView(ANSWER_TOOL, "Record every answer.", answer_schema(request)),),
            tool_choice=ANSWER_TOOL,
        )
        result = self._generation.generate(asked)
        call = next((c for c in result.tool_calls if c.name == ANSWER_TOOL), None)
        answers = call.arguments.get("answers") if call is not None else None
        found = answers if isinstance(answers, Mapping) else {}
        return ClassificationResult(
            tuple((q.name, _answer(q, found.get(q.name))) for q in request.questions),
            result.usage,
        )
