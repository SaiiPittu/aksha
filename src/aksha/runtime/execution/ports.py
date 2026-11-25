"""Protocols for durable execution (ADR-P02). Today: ``Clock``.

``Clock`` is the injected clock (``now``, ``sleep``, ``mono_ms``); its system and fake
implementations are adapters in :mod:`aksha.adapters.clock`. ``IdSource`` and
``DurableStepExecutor`` join this module when ADR-P02 is built.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """The injected clock: every recorded time, duration and backoff reads it."""

    def now(self) -> datetime:
        """The current time, timezone-aware, in UTC. Written into each event's ``at``."""
        ...

    def sleep(self, seconds: float) -> None:
        """Wait ``seconds``. Virtual under a fake clock: the time moves, nothing waits."""
        ...

    def mono_ms(self) -> int:
        """Real monotonic milliseconds in every mode; used for deadlines only (ADR-P02)."""
        ...
