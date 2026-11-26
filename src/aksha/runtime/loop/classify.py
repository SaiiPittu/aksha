"""Turn-end classification (ADR-P06 D6): pure functions over a reply and recorded outcomes.

1. :func:`classify_turn_end` applies the fixed priority when one turn produced several
   outcomes: ``approval``, ``interrupt``, ``budget``, ``final_answer``, ``tool_results``.
   A reply cut off by the output limit (or refused by the provider, or with neither text
   nor calls) is ``malformed`` and runs none of its calls.
2. :func:`model_failure` names the turn end of a model step that recorded no reply.
3. :func:`recorded_turn_end` rebuilds a turn's end from the step events alone, or says
   the turn is not finished. The loop returns exactly this value, so the turn end a
   restarted process finds is the one the live process returned (P-2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from aksha.runtime.context.types import ContextOverLimit
from aksha.runtime.events.envelope import Event
from aksha.runtime.execution.types import StepId
from aksha.runtime.loop.types import (
    SIGNAL_FIELD,
    SIGNALS,
    PositionOutcome,
    Signal,
    TurnEnd,
    TurnEndKind,
)
from aksha.runtime.models.codec import generation_result
from aksha.runtime.models.errors import ContextOverflow
from aksha.runtime.models.steps import MODEL_TRANSIENT, MODEL_UNAVAILABLE, RESULT_INVALID
from aksha.runtime.models.types import GenerationResult
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.types import InstanceRef

MALFORMED_FINISHES = frozenset({"length", "refusal"})
"""Finish reasons whose reply is not read: cut off by the output limit, or refused."""

BUDGET_EXHAUSTED = "budget_exhausted"
"""The gate's code for a call refused because a budget is spent (ADR-P11 layer 0)."""

CONTEXT_OVERFLOW = ContextOverflow.default_code
"""The code of a model step the provider refused as over its context window."""


def is_malformed(reply: GenerationResult) -> bool:
    """Whether ``reply`` cannot be read: cut off, refused, or with no text and no calls."""
    if reply.finish in MALFORMED_FINISHES:
        return True
    return not reply.tool_calls and not (reply.text or "").strip()


def classify_turn_end(reply: GenerationResult, positions: Sequence[PositionOutcome]) -> TurnEndKind:
    """The kind of a turn whose model step answered ``reply`` (module doc, point 1). Pure."""
    if is_malformed(reply):
        return "malformed"
    signals = {p.signal for p in positions}
    if "approval" in signals:
        return "approval"
    if "interrupt" in signals:
        return "interrupt"
    if "budget" in signals:
        return "budget"
    if not reply.tool_calls or "finish" in signals:
        return "final_answer"
    return "tool_results"


def model_failure(code: str | None) -> tuple[TurnEndKind, str]:
    """The turn end of a model step that ended with ``code`` and no reply. Pure."""
    if code == CONTEXT_OVERFLOW:
        return "context_overflow", CONTEXT_OVERFLOW
    if code == RESULT_INVALID:
        return "malformed", RESULT_INVALID
    if code in (MODEL_TRANSIENT, "timeout"):
        return "fault", MODEL_UNAVAILABLE
    return "fault", code or "model_failed"


def overflow_end(ref: InstanceRef, turn: int, over: ContextOverLimit) -> TurnEnd:
    """The turn end of a context over the hard limit: nothing was sent (ADR-P08 D6)."""
    return TurnEnd(ref.run, ref.instance, turn, "context_overflow", over.reason)


def position_outcome(position: int, tool: str, payload: Mapping[str, Any]) -> PositionOutcome:
    """One call position as its ``step.completed`` recorded it."""
    outcome = str(payload.get("outcome", ""))
    code = payload.get("code")
    value = payload.get("value")
    signal: Signal | None = None
    if isinstance(value, Mapping) and value.get(SIGNAL_FIELD) in SIGNALS:
        signal = cast(Signal, value[SIGNAL_FIELD])
    if outcome == "refused" and code == BUDGET_EXHAUSTED:
        signal = "budget"
    return PositionOutcome(position, tool, outcome, None if code is None else str(code), signal)


def _completed(view: LogView, prefix: str) -> tuple[dict[str, Event], int]:
    """The turn's ``step.completed`` events by step, and the seq of its last event."""
    done: dict[str, Event] = {}
    last = 0
    for e in view.events:
        if e.step is None or not (e.step == prefix or e.step.startswith(prefix + "/")):
            continue
        last = e.seq
        if e.type == "step.completed":
            done[e.step] = e
    return done, last


def recorded_turn_end(view: LogView, ref: InstanceRef, turn: int) -> TurnEnd | None:
    """Turn ``turn`` of ``ref``'s end as the log records it, or None while it is unfinished.

    Unfinished: the model step has no recorded end, or a call of its reply has none. A turn
    whose context was over the limit records nothing, so it reads as unfinished and runs
    again; assembling the same log again gives the same answer (ADR-P08 D6).
    """
    model = StepId(ref.run, ref.instance, turn=turn)
    done, last = _completed(view, str(model))
    end = done.get(str(model))
    if end is None:
        return None
    payload = end.payload
    if payload.get("outcome") != "ok":
        code = payload.get("code")
        kind, reason = model_failure(None if code is None else str(code))
        return TurnEnd(ref.run, ref.instance, turn, kind, reason, last)
    value = payload.get("value") or {}
    reply = generation_result(value.get("result", {}))
    if is_malformed(reply):
        return TurnEnd(ref.run, ref.instance, turn, "malformed", reply.finish, last)
    positions: list[PositionOutcome] = []
    for n, call in enumerate(reply.tool_calls, start=1):
        step = done.get(str(model.with_call(n)))
        if step is None:
            return None
        positions.append(position_outcome(n, call.name, step.payload))
    kind = classify_turn_end(reply, positions)
    return TurnEnd(ref.run, ref.instance, turn, kind, None, last)
