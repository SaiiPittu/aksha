"""The one event each gate decision becomes, with its reason (ADR-P11 D9). Pure.

An allow is recorded as the ``step.started`` that follows it (boundary U2a, step intent),
carrying the route and whether it counts against the tool-call budget. A refusal is one
``step.completed{outcome: refused, code, layer, message, detail}`` (boundary U2), and the
model reads the same message as an error result. There is no separate refusal event.

This module builds the event and names its boundary; it never appends. Both types are
written by the durable step executor (``writer="execution"``, ADR-P02), which appends
``decision_event(...)`` in ``boundary_of(...)``. The policy package holds no event store
handle, and ``tests/architecture`` checks it imports no storage port.

``tool_calls_used`` is the count budget accounting (ADR-P14) reads: allowed calls that
count plus refused calls that count, so a model looping on refusals still runs out.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from aksha.runtime.canonical import sha256_hex
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.policy.types import Allow, Refuse
from aksha.runtime.tools.types import ToolCall

STEP_INTENT = "U2a"
STEP_RESULT = "U2"


def args_hash(arguments: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` of the canonical JSON of ``arguments``."""
    return "sha256:" + sha256_hex(arguments)


def boundary_of(decision: Allow | Refuse) -> str:
    """The transaction boundary a decision is recorded in: U2a for an allow, U2 for a refusal."""
    return STEP_INTENT if isinstance(decision, Allow) else STEP_RESULT


def decision_event(
    decision: Allow | Refuse, call: ToolCall, *, run: str, instance: str, step: str
) -> NewEvent:
    """The one event that records ``decision`` on ``call`` for the step ``step``."""
    if isinstance(decision, Allow):
        payload: dict[str, Any] = {
            "kind": "tool",
            "tool": call.name,
            "args_hash": args_hash(call.arguments),
            "position": call.position,
            "route": decision.route,
            "counted": decision.counted,
        }
        return NewEvent("step.started", payload, run=run, instance=instance, step=step)
    payload = {
        "outcome": "refused",
        "tool": call.name,
        "code": decision.code,
        "layer": decision.layer,
        "message": decision.message,
        "counted": decision.counted,
        "detail": list(decision.detail),
    }
    return NewEvent("step.completed", payload, run=run, instance=instance, step=step)


def is_counted_tool_call(e: Event) -> bool:
    """Whether ``e`` uses one tool call: a counted tool start or a counted refusal."""
    if not bool(e.payload.get("counted")):
        return False
    if e.type == "step.started":
        return e.payload.get("kind") == "tool"
    return e.type == "step.completed" and e.payload.get("outcome") == "refused"


def tool_calls_used(events: Iterable[Event]) -> int:
    """How many tool calls count against the budget: counted starts plus counted refusals."""
    return sum(1 for e in events if is_counted_tool_call(e))
