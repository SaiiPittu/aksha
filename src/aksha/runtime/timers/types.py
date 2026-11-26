"""Timer values (ADR-P25): what a timer is, its scope, and one attempt's recorded time reads.

``TimerSet`` is the payload of ``timer.scheduled`` (D1). Its time is exactly one of
``due_at`` (an ISO time read from the injected clock) and ``after_ms`` (amendment B2:
milliseconds counted from the scheduling fact's own recorded ``at``, which also comes from
the injected clock, for a fact built inside a step where no clock reading is at hand).
Each cause names what its firing needs: an expiry its approval (``ApprovalRef``, D5), a
recheck its ``target`` (D3), a reminder its ``request`` (D2).

``Timer`` is the projection of one timer: its spec, when it is due, and whether it is
still pending. ``TimeReads`` (``reads.py``) collects one attempt's time reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.stop_policy.types import AgentScope, RunScope, SessionScope
from aksha.runtime.tasks.types import InstanceRef

TimerCause = Literal["approval_expiry", "reminder", "recheck"]
"""Why a timer exists: void an approval, start a reminder's run, expire a fetch or save."""

TIMER_CAUSES: frozenset[str] = frozenset(get_args(TimerCause))

TimerState = Literal["pending", "fired", "cancelled"]


@dataclass(frozen=True)
class ApprovalRef:
    """An approval as a timer's scope (D5): its expiry is not held by the run that asked."""

    approval: str


TimerScope = InstanceRef | RunScope | AgentScope | SessionScope | ApprovalRef
"""What a timer acts on: a hold scope (ADR-P16 S1), or an approval for its expiry."""


@dataclass(frozen=True)
class Reminder:
    """What a reminder's run is for: the agent that runs it and the request it states."""

    agent: str
    text: str


@dataclass(frozen=True)
class TimerSet:
    """One timer as ``timer.scheduled`` records it (module doc)."""

    timer_id: str
    scope: TimerScope
    cause: TimerCause
    due_at: str | None = None
    after_ms: int | None = None
    target: InputRef | None = None
    request: Reminder | None = None

    def __post_init__(self) -> None:
        """Refuse a timer whose time, cause or cause's fields are not all there."""
        if not self.timer_id or any(c.isspace() for c in self.timer_id):
            raise ValueError(f"a timer ID is non-empty text without spaces: {self.timer_id!r}")
        if self.cause not in TIMER_CAUSES:
            raise ValueError(f"a timer cause is one of {sorted(TIMER_CAUSES)}: {self.cause!r}")
        if (self.due_at is None) == (self.after_ms is None):
            raise ValueError("a timer names exactly one of due_at and after_ms")
        if self.after_ms is not None and self.after_ms < 0:
            raise ValueError("after_ms counts forward from the scheduling fact")
        if (self.cause == "approval_expiry") != isinstance(self.scope, ApprovalRef):
            raise ValueError("an approval_expiry timer, and only it, is scoped to its approval")
        if (self.cause == "recheck") != (self.target is not None):
            raise ValueError("a recheck timer, and only it, names its target")
        if self.target is not None and self.target.kind not in ("record", "artifact"):
            raise ValueError("a recheck target is a record read or an artifact version")
        if (self.cause == "reminder") != (self.request is not None):
            raise ValueError("a reminder timer, and only it, names the request its run states")
        if self.cause == "reminder" and not isinstance(self.scope, SessionScope):
            raise ValueError("a reminder is scoped to the session its run opens in")


@dataclass(frozen=True)
class Timer:
    """One timer in the projection: its spec, when it is due, and where it is in its life."""

    spec: TimerSet
    due_at: str
    scheduled_seq: int
    state: TimerState = "pending"
    closed_seq: int | None = None

    @property
    def pending(self) -> bool:
        """Whether neither ``timer.fired`` nor ``timer.cancelled`` closed it yet."""
        return self.state == "pending"
