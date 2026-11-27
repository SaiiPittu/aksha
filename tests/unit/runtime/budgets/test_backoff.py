"""The retry backoff (ADR-P14 D4, B2): doubling and capped, no jitter unless a live policy asks."""

from __future__ import annotations

from typing import Any

import pytest

from aksha.runtime.budgets.backoff import jittered, next_backoff_ms
from aksha.runtime.budgets.types import Backoff, Limits
from aksha.runtime.execution.types import RETRY_AFTER_CEILING_MS, StepPolicy


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
def test_backoff_refuses_a_shrinking_or_negative_delay(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Backoff(**bad)


def test_the_step_policy_delay_is_the_budget_backoff() -> None:
    policy = StepPolicy()
    assert [policy.delay_ms(a) for a in (1, 2, 3, 4)] == [
        next_backoff_ms(a, policy.backoff) for a in (1, 2, 3, 4)
    ]


# --- live backoff: jitter and Retry-After (ADR-P14 B2) -----------------------------------

LIVE = Backoff(base_ms=2_000, factor=2, cap_ms=60_000)


def test_no_jitter_by_default_so_fake_runs_sleep_the_same_delays() -> None:
    assert (StepPolicy().jitter, Limits().jitter) == (0.0, 0.0)
    policy = StepPolicy(backoff=LIVE, jitter_source=lambda: 0.99)  # no jitter: never drawn
    assert [policy.delay_ms(a) for a in range(1, 8)] == [
        2_000, 4_000, 8_000, 16_000, 32_000, 60_000, 60_000,
    ]  # fmt: skip


@pytest.mark.parametrize(("draw", "delays"), [
    (0.0, [2_000, 4_000, 8_000, 60_000]),
    (1.0, [1_500, 3_000, 6_000, 45_000]),
    (0.5, [1_750, 3_500, 7_000, 52_500]),
])  # fmt: skip
def test_jitter_cuts_each_delay_by_up_to_its_share_never_above_the_cap(
    draw: float, delays: list[int]
) -> None:
    policy = StepPolicy(backoff=LIVE, jitter=0.25, jitter_source=lambda: draw)
    assert [policy.delay_ms(a) for a in (1, 2, 3, 9)] == delays
    assert [jittered(d, 0.25, draw) for d in (2_000, 60_000)] == [delays[0], delays[3]]


@pytest.mark.parametrize("jitter", [-0.1, 1.5])
def test_jitter_is_a_share_from_zero_to_one(jitter: float) -> None:
    with pytest.raises(ValueError, match="jitter"):
        StepPolicy(jitter=jitter, jitter_source=lambda: 0.5)
    with pytest.raises(ValueError, match="jitter"):
        Limits(jitter=jitter, jitter_source=lambda: 0.5)


def test_jitter_needs_an_injected_random_source() -> None:
    # the runtime reads no ambient randomness (ADR-P02 D3)
    with pytest.raises(ValueError, match="jitter_source"):
        StepPolicy(jitter=0.25)
    with pytest.raises(ValueError, match="jitter_source"):
        Limits(jitter=0.25)


def test_a_retry_after_lengthens_the_backoff_never_shortens_it() -> None:
    policy = StepPolicy(backoff=LIVE)
    assert policy.delay_ms(1, retry_after_ms=30_000) == 30_000
    assert policy.delay_ms(5, retry_after_ms=1_000) == 32_000  # the backoff is longer
    assert policy.delay_ms(1, retry_after_ms=None) == 2_000
    assert policy.delay_ms(1, retry_after_ms=10**9) == RETRY_AFTER_CEILING_MS
