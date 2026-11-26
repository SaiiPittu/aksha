"""The route question, its request and the clarifying question (ADR-P23 D1, D3). Pure.

The router is one classification call under :data:`~aksha.runtime.router.types.ROUTE_ALIAS`.
Its one question's options are the loaded agents' names, in declared order, and each
option's text is the agent's ``purpose`` line, rendered by code into the question's
prompt (one ``- <name>: <purpose>`` line per agent, read back by :func:`option_texts`).
The state the classifier sees is the message text only (RT-1 A): no earlier transcript
enters it, so a route never depends on history and the same words hash the same.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from aksha.runtime.models.types import (
    ClassificationRequest,
    ConfidenceThresholds,
    ModelAlias,
    QuestionSpec,
)
from aksha.runtime.router.types import ROUTE_ALIAS, ROUTE_QUESTION, RoutableAgent

PROMPT_HEAD = "Which agent should handle this request?"
_ITEM = "- "
_SEP = ": "


def route_prompt(agents: Sequence[RoutableAgent]) -> str:
    """The question's prompt: the head, then one ``- <name>: <purpose>`` line per agent."""
    lines = [PROMPT_HEAD, *(f"{_ITEM}{a.name}{_SEP}{a.purpose.strip()}" for a in agents)]
    return "\n".join(lines)


def route_question(
    agents: Sequence[RoutableAgent], thresholds: ConfidenceThresholds
) -> QuestionSpec:
    """The one route question: a choice among the agents' names, judged by ``thresholds``."""
    return QuestionSpec(
        ROUTE_QUESTION, route_prompt(agents), "choice",
        options=tuple(a.name for a in agents), thresholds=thresholds,
    )  # fmt: skip


def route_alias(agents: Sequence[RoutableAgent], thresholds: ConfidenceThresholds) -> ModelAlias:
    """The runtime-owned classification alias the router calls (D1). Never adapted."""
    return ModelAlias(
        ROUTE_ALIAS, "classification", questions=(route_question(agents, thresholds),)
    )


def option_texts(question: QuestionSpec) -> dict[str, str]:
    """Each option's text (its purpose line), read back from the rendered prompt."""
    out: dict[str, str] = {}
    for line in question.prompt.splitlines()[1:]:
        if not line.startswith(_ITEM) or _SEP not in line:
            continue
        name, text = line[len(_ITEM) :].split(_SEP, 1)
        if name in question.options:
            out[name] = text
    return out


def route_request(step_id: str, text: str, question: QuestionSpec) -> ClassificationRequest:
    """The classification request for one message: its text is the whole state (RT-1 A)."""
    return ClassificationRequest(step_id, ROUTE_ALIAS, text, (question,))


def _phrase(purpose: str, first: bool) -> str:
    text = purpose.strip().rstrip(".?!").strip()
    if not first and len(text) > 1 and text[0].isupper() and text[1].islower():
        text = text[0].lower() + text[1:]
    return text


def clarifying_question(ranked: Sequence[str], purposes: Mapping[str, str]) -> str:
    """The question a route interrupt asks: the top agents' purposes as one "or" question.

    ``("a", "b")`` with purposes "Do x." and "Do y." asks "Do x, or do y?".
    """
    phrases = [_phrase(purposes[name], i == 0) for i, name in enumerate(ranked)]
    if len(phrases) == 1:
        return f"{phrases[0]}?"
    return f"{', '.join(phrases[:-1])}, or {phrases[-1]}?"
