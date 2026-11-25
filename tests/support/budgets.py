"""Hand-built logs for the budget and limit reducers: stored events without a store.

``Log`` numbers each event and stamps it a second after the previous one, so a test reads
like the run it describes. ``view()`` is a complete ``LogView`` of what was added.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from aksha.runtime.canonical import iso_utc
from aksha.runtime.events.envelope import Event
from aksha.runtime.projections.view import ArtifactsView, LogView
from tests.support.events import START

RUN = "R-0001"


class Log:
    """An owner's events, appended in order by the helpers below."""

    def __init__(self, owner: str = "o") -> None:
        self.owner = owner
        self.events: list[Event] = []

    def add(
        self,
        type_: str,
        payload: Mapping[str, Any] | None = None,
        *,
        run: str | None = RUN,
        instance: str | None = None,
        step: str | None = None,
        at_s: float | None = None,
    ) -> Event:
        seq = len(self.events) + 1
        moment = START + timedelta(seconds=seq if at_s is None else at_s)
        event = Event(
            type_, dict(payload or {}), run=run, instance=instance, step=step,
            seq=seq, owner=self.owner, at=iso_utc(moment), boot=1, schema_version=1,
        )  # fmt: skip
        self.events.append(event)
        return event

    def view(self) -> LogView:
        seq = len(self.events)
        return LogView(self.owner, seq, tuple(self.events), ArtifactsView((), seq), complete=True)

    # --- the facts the reducers read -------------------------------------------------

    def started(self, budget: Mapping[str, int] | None = None, run: str = RUN) -> Event:
        payload: dict[str, Any] = {"agent": "a", "cause": "request"}
        if budget is not None:
            payload["budget"] = dict(budget)
        return self.add("run.started", payload, run=run)

    def decided(self, verdict: str = "continue", *, instance: str = "a", **fields: Any) -> Event:
        walk = 1 + sum(1 for e in self.events if e.type == "stop.decided")
        payload = {"walk": walk, "verdict": verdict, **fields}
        return self.add("stop.decided", payload, instance=instance)

    def tool(self, step: str, *, counted: bool = True, instance: str = "a") -> Event:
        payload = {"kind": "tool", "tool": "fetch", "counted": counted}
        return self.add("step.started", payload, instance=instance, step=step)

    def refused(self, step: str, *, counted: bool = True, code: str = "not_allowed") -> Event:
        payload = {"outcome": "refused", "tool": "fetch", "code": code, "counted": counted}
        return self.add("step.completed", payload, instance="a", step=step)

    def failed(self, step: str, attempt: int, *, duration: int = 0, backoff: int = 0) -> Event:
        payload = {
            "attempt": attempt, "code": "timeout", "retried": backoff > 0,
            "backoff_ms": backoff, "duration_ms": duration,
        }  # fmt: skip
        return self.add("step.failed", payload, instance="a", step=step)

    def completed(
        self,
        step: str,
        *,
        kind: str = "tool",
        outcome: str = "ok",
        duration: int = 0,
        usage: Mapping[str, int] | None = None,
        instance: str = "a",
    ) -> Event:
        payload: dict[str, Any] = {"outcome": outcome, "kind": kind, "duration_ms": duration}
        if outcome == "error":
            payload["code"] = "timeout"
        if usage is not None:
            payload["usage"] = dict(usage)
        return self.add("step.completed", payload, instance=instance, step=step)

    def granted(self, extra: Mapping[str, int], interrupt_id: str = "I-0001") -> Event:
        payload = {"interrupt_id": interrupt_id, "by": "person", "extra": dict(extra)}
        return self.add("budget.granted", payload)

    def resumed(self) -> Event:
        return self.decided("continue", instance="a", cause="resume", resumes_turn=True)

    def entered(self, instance: str = "a") -> Event:
        return self.add("task.entered", {"cause": "scheduler"}, instance=instance)

    def saved(self, ref: str = "note/A@v1") -> Event:
        return self.add("artifact.saved", {"ref": ref, "kind": "note", "key": "A", "version": 1})
