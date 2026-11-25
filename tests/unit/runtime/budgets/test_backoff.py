"""The fixed backoff (ADR-P14 D4): deterministic, doubling, capped, no jitter."""

from __future__ import annotations

import pytest

from aksha.runtime.budgets.backoff import next_backoff_ms
from aksha.runtime.budgets.types import Backoff
from aksha.runtime.execution.types import StepPolicy


def test_backoff_doubles_from_the_base_to_the_cap() -> None:
    b = Backoff()
    assert [next_backoff_ms(a, b) for a in range(1, 7)] == [500, 1_000, 2_000, 4_000, 4_000, 4_000]


def test_backoff_is_the_same_every_time() -> None:
    b = Backoff(base_ms=300, factor=3, cap_ms=5_000)
    first = [next_backoff_ms(a, b) for a in range(1, 10)]
    assert first == [next_backoff_ms(a, b) for a in range(1, 10)]
    assert first[:4] == [300, 900, 2_700, 5_000]


def test_a_large_attempt_does_not_overflow_past_the_cap() -> None:
    assert next_backoff_ms(10_000, Backoff()) == 4_000


def test_attempts_are_counted_from_one() -> None:
    with pytest.raises(ValueError, match="from 1"):
        next_backoff_ms(0, Backoff())


@pytest.mark.parametrize("bad", [{"base_ms": -1}, {"factor": 0}, {"base_ms": 500, "cap_ms": 100}])
def test_backoff_refuses_a_shrinking_or_negative_delay(bad: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        Backoff(**bad)


def test_the_step_policy_delay_is_the_budget_backoff() -> None:
    policy = StepPolicy()
    assert [policy.delay_ms(a) for a in (1, 2, 3, 4)] == [
        next_backoff_ms(a, policy.backoff) for a in (1, 2, 3, 4)
    ]
