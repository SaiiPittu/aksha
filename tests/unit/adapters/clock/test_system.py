from datetime import UTC

from aksha.adapters.clock.system import SystemClock
from aksha.runtime.execution.ports import Clock


def test_system_clock_is_utc_and_monotonic() -> None:
    c: Clock = SystemClock()
    assert c.now().tzinfo is UTC
    a = c.mono_ms()
    c.sleep(0.001)
    assert c.mono_ms() >= a
