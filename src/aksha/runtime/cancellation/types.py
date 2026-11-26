"""Cancellation values (ADR-P18): the stop scope, a stop request, its holds and the kill policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

StopScope = Literal["run", "turn"]
"""What a stop ends: ``run`` ends the run; ``turn`` (designed only) ends the current turn."""

STOP_SCOPES = frozenset({"run", "turn"})

DEFAULT_REASON = "Stopped by user"
"""The reason a stop records when the person gives none."""

STOP_REQUEST = "run.stop_requested"
"""The event type of a stop request."""

STOPPED = "stopped"
"""The ``run.ended`` outcome of a run a person stopped; never ``partial``."""

STOP_REQUESTED = "stop_requested"
"""The ``run.ended`` and ``stop.decided`` reason of a stopped run, and the run's hold reason."""

STOPPED_BY_PERSON = "stopped_by_person"
"""The hold reason of an (owner, agent) after a person's stop, until their next command (D4)."""

RUN_STOPPED = "run_stopped"
"""The code of a step a stop skipped or cancelled, and the reason an interrupt is voided."""

HoldReason = Literal["stop_requested", "stopped_by_person"]
"""The hold reasons cancellation owns; ADR-P16's ``hold_reason`` adds ``interrupt_open``."""

KillOutcome = Literal["settled", "killed"]
"""How the kill boundary ended a unit: it settled on request, or was terminated."""


@dataclass(frozen=True)
class StopRequestView:
    """One recorded stop request.

    ``by`` is the principal who asked; ``seq`` is the request's sequence number;
    ``honoured_by`` is the seq of the ``stop.decided`` that ended the run, or None.
    """

    run: str
    scope: StopScope
    reason: str
    by: str
    command_id: str
    seq: int
    honoured_by: int | None = None


@dataclass(frozen=True)
class StopHold:
    """Why a scope is held by a person's stop.

    ``scope`` is a run ID for ``stop_requested`` and an agent name for
    ``stopped_by_person``; ``by`` is the seq of the stop request that holds it.
    """

    reason: HoldReason
    scope: str
    by: int


@dataclass(frozen=True)
class HeldStart:
    """An automatic start the log records while its scope was held: a broken TC-4 invariant."""

    seq: int
    type: str
    run: str | None
    reason: HoldReason | Literal["interrupt_open"]


@dataclass(frozen=True)
class KillPolicy:
    """How long the kill boundary waits (D6).

    ``grace_seconds`` is the wait after asking a unit to settle, before it is terminated (a
    placeholder of 5 seconds, not measured); ``reap_seconds`` is the wait after terminating.
    """

    grace_seconds: float = 5.0
    reap_seconds: float = 1.0

    def __post_init__(self) -> None:
        """Refuse a negative wait."""
        if self.grace_seconds < 0 or self.reap_seconds < 0:
            raise ValueError("the kill boundary's waits are not negative")
