"""``SystemClock``: real UTC time and a real monotonic clock."""

from __future__ import annotations

import time
from datetime import UTC, datetime


class SystemClock:
    """The production :class:`~aksha.runtime.execution.ports.Clock`."""

    def now(self) -> datetime:
        """The wall-clock time in UTC."""
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        """Block for ``seconds``."""
        time.sleep(seconds)

    def mono_ms(self) -> int:
        """Monotonic milliseconds."""
        return time.monotonic_ns() // 1_000_000
