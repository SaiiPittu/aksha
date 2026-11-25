"""``FakeClock``: a fixed clock that moves only when told; ``sleep`` is virtual.

Used by tests, fake model mode and replay, so recorded times are deterministic. Following
ADR-P02, ``mono_ms`` stays real: deadlines measure real elapsed time in every mode.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta


class FakeClock:
    """A :class:`~aksha.runtime.execution.ports.Clock` whose ``now`` moves only on request."""

    def __init__(self, start: datetime) -> None:
        """Start at ``start``, which must be timezone-aware."""
        if start.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware start")
        self._now = start

    def now(self) -> datetime:
        """The current fake time."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the fake time forward by ``seconds`` (never backward)."""
        if seconds < 0:
            raise ValueError("a clock never moves backward")
        self._now += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        """Advance the fake time by ``seconds`` without waiting."""
        self.advance(seconds)

    def mono_ms(self) -> int:
        """Real monotonic milliseconds (ADR-P02: real in every mode)."""
        return time.monotonic_ns() // 1_000_000
