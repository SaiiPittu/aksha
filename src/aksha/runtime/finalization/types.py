"""Partial-result values (ADR-P24): an unknown entry, the finalizing record and the reserve.

``UnknownEntry`` is one explicit unknown a run's output holds instead of a value: the
instance whose output is missing, why (``unverified``: it or an instance it needs is
blocked, D2; ``not_checked``: finalization voided it before it finished, D4) and what
blocked it. ``Finalizing`` is the ``run.finalizing`` fact read back. The reserve's share
is a placeholder (PF-2, lean A): a fixed share of each capped dimension, and a number of
model turns per finalizer Task, never more than half of a dimension's allowance.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

UnknownCause = Literal["unverified", "not_checked"]
"""Why a run's output holds an unknown: a tolerated block (D2) or finalization (D4)."""

RESERVE_SHARE_PERCENT = 10
"""The share of each capped dimension set aside for finalizer Tasks (D5, placeholder)."""

RESERVE_TURNS_PER_FINALIZER = 2
"""The model turns set aside per finalizer Task (D5, placeholder)."""

RESERVE_MAX_PERCENT = 50
"""The reserve never takes more than this share of a dimension's allowance."""


@dataclass(frozen=True)
class UnknownEntry:
    """One explicit unknown of a run's output (D2, D4, D6).

    ``key`` is the instance whose output is missing (``fetch_extract[COMP-04]``);
    ``blocker`` is the blocked instance behind an ``unverified`` entry (itself, or an
    instance it needs), or the budget interrupt finalization answered for ``not_checked``.
    """

    key: str
    reason: UnknownCause
    blocker: str | None = None

    def as_json(self) -> dict[str, Any]:
        """The entry as ``run.ended{unknowns}`` records it."""
        out: dict[str, Any] = {"key": self.key, "reason": self.reason}
        if self.blocker is not None:
            out["blocker"] = self.blocker
        return out


@dataclass(frozen=True)
class Finalizing:
    """A run's ``run.finalizing`` fact: who answered which interrupt, the reserve and lists.

    ``finalizers`` are the finalizer Task paths in declared order; ``voided`` the
    instances finalization voided when it was recorded (later keys are voided by the same
    rule, ``tasks.status``).
    """

    run: str
    interrupt: str
    by: str
    reserve: Mapping[str, int]
    finalizers: tuple[str, ...]
    voided: tuple[str, ...]
    seq: int
