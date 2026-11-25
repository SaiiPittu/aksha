from datetime import UTC, datetime, timedelta

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.runtime.execution.ports import Clock


def test_fake_clock_is_fixed_until_advanced() -> None:
    start = datetime(2026, 11, 27, tzinfo=UTC)
    c = FakeClock(start)
    assert c.now() == start
    c.advance(1.5)
    assert c.now() == start + timedelta(seconds=1.5)


def test_fake_clock_sleep_is_virtual() -> None:
    c: Clock = FakeClock(datetime(2026, 11, 27, tzinfo=UTC))
    before = c.mono_ms()
    c.sleep(3600)
    assert c.now() == datetime(2026, 11, 27, 1, 0, 0, tzinfo=UTC)
    # ADR-P02: mono_ms is real in every mode, so a virtual hour costs no real time.
    assert c.mono_ms() - before < 1000


def test_fake_clock_refuses_naive_start_and_negative_steps() -> None:
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 11, 27))
    c = FakeClock(datetime(2026, 11, 27, tzinfo=UTC))
    with pytest.raises(ValueError):
        c.advance(-1)
    with pytest.raises(ValueError):
        c.sleep(-1)
