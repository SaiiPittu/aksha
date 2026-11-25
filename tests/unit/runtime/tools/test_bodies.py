from collections.abc import Mapping
from typing import Any

import pytest

from aksha.runtime.tools.bodies import call_body
from aksha.runtime.tools.types import (
    Err,
    Ok,
    RetryPolicy,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
)

CTX = ToolContext(owner="o", agent="a", task_path="t", key=None, step_id="s", attempt=1)
SPEC = ToolSpec(name="t", owner="a", effect="read", description="d", schema={"type": "object"})


def test_tool_exception_is_a_result() -> None:
    calls: list[Mapping[str, Any]] = []

    def boom(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        calls.append(args)
        raise KeyError("missing")

    outcome = call_body(ToolDecl(SPEC, boom), CTX, {"x": 1})
    assert isinstance(outcome.result, Err) and outcome.result.code == "tool_exception"
    assert "KeyError" in outcome.result.message
    assert len(calls) == 1, "a raising body runs once"
    assert "tool_exception" not in RetryPolicy().retry_on, "a bug repeats, so it is not retried"


def test_body_gets_frozen_arguments() -> None:
    def mutate(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        args["x"] = 2  # type: ignore[index]
        return ToolOutcome(Ok({}))

    args = {"x": 1}
    outcome = call_body(ToolDecl(SPEC, mutate), CTX, args)
    assert isinstance(outcome.result, Err) and outcome.result.code == "tool_exception"
    assert args == {"x": 1}


def test_ok_outcome_passes_through() -> None:
    def echo(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        return ToolOutcome(Ok({"x": args["x"], "owner": ctx.owner}))

    outcome = call_body(ToolDecl(SPEC, echo), CTX, {"x": 1})
    assert outcome == ToolOutcome(Ok({"x": 1, "owner": "o"}))


@pytest.mark.parametrize("returned", [{"x": 1}, None, Ok({"x": 1})])
def test_a_body_that_returns_no_outcome_is_a_class_violation(returned: object) -> None:
    def wrong(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        return returned  # type: ignore[return-value]

    outcome = call_body(ToolDecl(SPEC, wrong), CTX, {})
    assert isinstance(outcome.result, Err) and outcome.result.code == "class_violation"


def test_a_tool_without_a_body_is_a_class_violation() -> None:
    outcome = call_body(ToolDecl(SPEC, None), CTX, {})
    assert isinstance(outcome.result, Err) and outcome.result.code == "class_violation"
