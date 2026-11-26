"""The three timer facts (ADR-P25 D1) and the scope's stored form. Pure: builds, never appends.

``timer.scheduled`` is written in the transaction that wants the timer, by its caller;
``timer.fired`` and ``timer.cancelled`` share the dedup key ``tclose:{timer_id}``, so a
timer closes once, whichever comes first, however often a crashed firing is retried.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.stop_policy.types import AgentScope, RunScope, SessionScope
from aksha.runtime.tasks.types import InstanceRef
from aksha.runtime.timers.types import ApprovalRef, Reminder, TimerCause, TimerScope, TimerSet

TIMER_SCHEDULED = "timer.scheduled"
TIMER_FIRED = "timer.fired"
TIMER_CANCELLED = "timer.cancelled"
MOOT = "moot"
"""The cancellation reason of a timer whose target was already settled when it came due."""


def scope_json(scope: TimerScope) -> dict[str, str]:
    """A scope as ``timer.scheduled`` stores it."""
    if isinstance(scope, InstanceRef):
        return {"kind": "instance", "run": scope.run, "instance": scope.instance}
    if isinstance(scope, RunScope):
        return {"kind": "run", "run": scope.run}
    if isinstance(scope, AgentScope):
        return {"kind": "agent", "agent": scope.agent}
    if isinstance(scope, SessionScope):
        return {"kind": "session", "session": scope.session}
    return {"kind": "approval", "approval": scope.approval}


def scope_from_json(value: Mapping[str, Any]) -> TimerScope:
    """The scope a stored ``scope`` names."""
    kind = value["kind"]
    if kind == "instance":
        return InstanceRef.parse(str(value["run"]), str(value["instance"]))
    if kind == "run":
        return RunScope(str(value["run"]))
    if kind == "agent":
        return AgentScope(str(value["agent"]))
    if kind == "session":
        return SessionScope(str(value["session"]))
    return ApprovalRef(str(value["approval"]))


def timer_json(timer: TimerSet) -> dict[str, Any]:
    """The payload of ``timer.scheduled`` for ``timer``."""
    payload: dict[str, Any] = {
        "timer_id": timer.timer_id,
        "cause": timer.cause,
        "scope": scope_json(timer.scope),
    }
    if timer.due_at is not None:
        payload["due_at"] = timer.due_at
    if timer.after_ms is not None:
        payload["after_ms"] = timer.after_ms
    if timer.target is not None:
        payload["target"] = timer.target.as_json()
    if timer.request is not None:
        payload["request"] = {"agent": timer.request.agent, "text": timer.request.text}
    return payload


def timer_from_json(payload: Mapping[str, Any]) -> TimerSet:
    """The timer a ``timer.scheduled`` payload records."""
    target = payload.get("target")
    request = payload.get("request")
    return TimerSet(
        timer_id=str(payload["timer_id"]),
        scope=scope_from_json(payload["scope"]),
        cause=payload["cause"],
        due_at=None if payload.get("due_at") is None else str(payload["due_at"]),
        after_ms=None if payload.get("after_ms") is None else int(payload["after_ms"]),
        target=None if target is None else InputRef.from_json(target),
        request=None if request is None else Reminder(str(request["agent"]), str(request["text"])),
    )


def scheduled(
    timer: TimerSet,
    *,
    run: str | None = None,
    instance: str | None = None,
    step: str | None = None,
) -> NewEvent:
    """``timer.scheduled`` for ``timer``, in the envelope of the step or run that wants it."""
    return NewEvent(TIMER_SCHEDULED, timer_json(timer), run=run, instance=instance, step=step)


def fired(timer_id: str, cause: TimerCause, *, run: str | None = None) -> NewEvent:
    """``timer.fired``; ``run`` is the run a reminder opened."""
    payload: dict[str, Any] = {"timer_id": timer_id, "cause": cause}
    if run is not None:
        payload["run"] = run
    return NewEvent(TIMER_FIRED, payload)


def cancelled(timer_id: str, reason: str = MOOT) -> NewEvent:
    """``timer.cancelled`` with its reason (snake case)."""
    return NewEvent(TIMER_CANCELLED, {"timer_id": timer_id, "reason": reason})
