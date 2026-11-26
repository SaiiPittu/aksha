"""Turn-end classification (ADR-P06 D6): the fixed priority, malformed replies, rebuilt ends."""

from __future__ import annotations

import pytest

from aksha.runtime.context.types import ContextOverLimit
from aksha.runtime.loop.classify import (
    classify_turn_end,
    is_malformed,
    model_failure,
    overflow_end,
    position_outcome,
)
from aksha.runtime.loop.types import PositionOutcome, Signal
from aksha.runtime.models.types import GenerationResult, ToolCall
from aksha.runtime.tasks.types import InstanceRef

CALLS = (ToolCall("a", "ask"), ToolCall("b", "send"), ToolCall("c", "finish"))
REPLY = GenerationResult(None, CALLS, "tool_calls")


def _positions(*signals: Signal | None) -> tuple[PositionOutcome, ...]:
    return tuple(PositionOutcome(n, f"t{n}", "ok", None, s) for n, s in enumerate(signals, 1))


def test_turn_end_priority() -> None:
    """A turn with an approval request, a question and a finish ends ``approval``."""
    assert classify_turn_end(REPLY, _positions("interrupt", "approval", "finish")) == "approval"
    assert classify_turn_end(REPLY, _positions("finish", "interrupt", "budget")) == "interrupt"
    assert classify_turn_end(REPLY, _positions("finish", "budget", None)) == "budget"
    assert classify_turn_end(REPLY, _positions(None, None, "finish")) == "final_answer"
    assert classify_turn_end(REPLY, _positions(None, None, None)) == "tool_results"


def test_text_without_calls_is_a_final_answer() -> None:
    assert classify_turn_end(GenerationResult("All done.", (), "text"), ()) == "final_answer"


@pytest.mark.parametrize(
    "reply",
    [
        GenerationResult("cut", CALLS, "length"),
        GenerationResult("no", (), "refusal"),
        GenerationResult(None, (), "text"),
        GenerationResult("   ", (), "text"),
    ],
)
def test_length_refusal_and_empty_replies_are_malformed(reply: GenerationResult) -> None:
    assert is_malformed(reply)
    assert classify_turn_end(reply, _positions("approval")) == "malformed"


@pytest.mark.parametrize(
    ("code", "kind", "reason"),
    [
        ("context_overflow", "context_overflow", "context_overflow"),
        ("result_invalid", "malformed", "result_invalid"),
        ("model_transient", "fault", "model_unavailable"),
        ("timeout", "fault", "model_unavailable"),
        ("run_stopped", "fault", "run_stopped"),
        (None, "fault", "model_failed"),
    ],
)
def test_model_failures_name_their_turn_end(code: str | None, kind: str, reason: str) -> None:
    assert model_failure(code) == (kind, reason)


def test_a_budget_refusal_signals_budget() -> None:
    refused = {"outcome": "refused", "code": "budget_exhausted"}
    assert position_outcome(2, "run_tests", refused).signal == "budget"
    other = {"outcome": "refused", "code": "unknown_tool"}
    assert position_outcome(2, "nope", other).signal is None


def test_a_recorded_signal_is_read_from_the_value() -> None:
    payload = {"outcome": "ok", "value": {"view": {}, "signal": "approval"}}
    assert position_outcome(1, "send", payload).signal == "approval"
    assert position_outcome(1, "send", {"outcome": "ok", "value": {"signal": "x"}}).signal is None


def test_an_overflow_names_the_limit() -> None:
    end = overflow_end(InstanceRef("R-0001", "edit"), 4, ContextOverLimit(120, 100))
    assert (end.kind, end.reason, end.turn) == ("context_overflow", "context_limit", 4)
