"""``next_backoff_ms``: the fixed retry backoff, a pure function of the attempt (ADR-P14 D4).

No jitter and no clock: two fake runs sleep the same virtual delays, so their logs match.
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
