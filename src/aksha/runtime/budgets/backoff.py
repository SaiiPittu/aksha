"""``next_backoff_ms``: the fixed retry backoff, a pure function of the attempt (ADR-P14 D4).

No jitter and no clock: two fake runs sleep the same virtual delays, so their logs match.
``jittered`` cuts a delay by a share drawn from an injected source (ADR-P14 B2), which
only a live model step's policy asks for.
"""

from __future__ import annotations

from aksha.runtime.budgets.types import Backoff


def next_backoff_ms(attempt: int, b: Backoff) -> int:
    """The delay after failed ``attempt`` (from 1): ``base * factor ** (attempt - 1)``, capped."""
    if attempt < 1:
        raise ValueError(f"attempts are counted from 1, not {attempt}")
    delay = b.base_ms
    for _ in range(attempt - 1):
        if delay >= b.cap_ms:
            break
        delay *= b.factor
    return min(b.cap_ms, delay)


def jittered(delay_ms: int, share: float, draw: float) -> int:
    """``delay_ms`` cut by up to ``share`` of itself: ``draw`` (in [0, 1)) picks how much.

    Never above ``delay_ms``, never below ``(1 - share)`` of it.
    """
    if share <= 0 or delay_ms <= 0:
        return delay_ms
    return max(0, round(delay_ms * (1.0 - share * min(1.0, max(0.0, draw)))))
