"""Budget values: ``Dimension``, ``Backoff``, ``Budget``, ``Limits`` and ``LimitHit`` (ADR-P14).

``Limits`` holds the turn, veto and restart limits; the restart limit counts resumes
since the last progress (ADR-P14 S2). Nothing here counts anything: every count is a
reducer over the event store (``meter.py``, ``limits.py``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from typing import Literal

Dimension = Literal["model_turns", "tool_calls", "active_ms", "tokens", "cost_micro_usd"]
"""One dimension of a run's budget."""

DIMENSIONS: tuple[Dimension, ...] = (
    "model_turns",
    "tool_calls",
    "active_ms",
    "tokens",
    "cost_micro_usd",
)
"""Every dimension, in the order budget accounting reports them."""

LimitName = Literal["turn_limit", "stop_veto_limit", "restart_limit"]
"""A limit inside a run that is not a budget dimension (ADR-P14 D7)."""


@dataclass(frozen=True)
class Backoff:
    """Retry delays: ``base_ms`` times ``factor`` per attempt, capped. No jitter, for replay.

    A live model step adds its jitter through ``Limits.jitter`` (ADR-P14 B2).
    """

    base_ms: int = 500
    factor: int = 2
    cap_ms: int = 4_000

    def __post_init__(self) -> None:
        """Refuse a negative delay, a shrinking factor, and a cap below the base."""
        if self.base_ms < 0 or self.factor < 1 or self.cap_ms < self.base_ms:
            raise ValueError("base_ms >= 0, factor >= 1 and cap_ms >= base_ms")


@dataclass(frozen=True)
class Budget:
    """The caps of an agent, a Task, or the runtime default. ``None`` means not capped.

    Turns, tool calls and active time are always capped; tokens and money are capped only
    when set (ADR-P14 D9).
    """

    model_turns: int = 25
    tool_calls: int = 40
    active_ms: int = 120_000
    tokens: int | None = None
    cost_micro_usd: int | None = None

    def __post_init__(self) -> None:
        """Refuse a negative cap."""
        for f in fields(self):
            value = getattr(self, f.name)
            if value is not None and value < 0:
                raise ValueError(f"budget {f.name} is {value}; a cap is not negative")

    def cap(self, d: Dimension) -> int | None:
        """The cap on dimension ``d``, or None when it is not capped."""
        value: int | None = getattr(self, d)
        return value

    def as_payload(self) -> dict[str, int]:
        """The capped dimensions, for ``run.started{budget}``; an uncapped one is left out."""
        return {d: v for d in DIMENSIONS if (v := self.cap(d)) is not None}

    @classmethod
    def from_payload(cls, payload: object) -> Budget:
        """The budget ``as_payload`` recorded on ``run.started``.

        A token or money cap left out is not capped; a turn, call or time cap left out
        keeps the default (those three are always capped).
        """
        values = payload if isinstance(payload, Mapping) else {}
        held = {d: int(values[d]) for d in DIMENSIONS if d in values}
        return cls(**held)

    def narrow(self, other: Budget) -> Budget:
        """The field-wise minimum of two budgets; a ``None`` field yields to a number."""
        values: dict[str, int | None] = {}
        for f in fields(self):
            mine, theirs = getattr(self, f.name), getattr(other, f.name)
            values[f.name] = (
                theirs if mine is None else mine if theirs is None else min(mine, theirs)
            )
        return Budget(**values)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Limits:
    """Turn, veto, restart, retry and timeout limits, each counted from the event store.

    ``max_retries``, ``backoff`` and ``jitter`` are the generation model step's retries
    (ADR-P14 amendment B2): by default two retries from 500 ms with no jitter, so a fake
    run sleeps the same delays every time; live mode raises them. ``jitter`` is the share
    of each delay drawn at random (0 to 1: with 0.25 a 4 s delay is 3 to 4 s), from
    ``jitter_source`` (a draw in [0, 1)): the runtime reads no ambient randomness
    (ADR-P02 D3), so the composition root injects it, and jitter without one is refused.
    """

    turn_limit: int = 10
    stop_veto_limit: int = 3
    restart_limit: int = 3
    auto_starts: tuple[int, int] = (3, 300_000)
    max_retries: int = 2
    tool_timeout_ms: int = 10_000
    model_timeout_ms: int = 60_000
    backoff: Backoff = Backoff()
    jitter: float = 0.0
    jitter_source: Callable[[], float] | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        """Refuse a jitter outside 0 to 1, or one with no random source to draw from."""
        if not 0.0 <= self.jitter <= 1.0:
            raise ValueError("jitter is a share of the delay, from 0 to 1")
        if self.jitter > 0 and self.jitter_source is None:
            raise ValueError("a jittered backoff needs its random source (jitter_source)")


@dataclass(frozen=True)
class LimitHit:
    """A limit that bites: which one, the count behind it and what was allowed.

    ``scope`` is the Task instance for the turn and stop-veto limits, None for the run.
    Limit hits are entries in a stop decision's trail, never events of their own.
    """

    limit: LimitName
    used: int
    allowed: int
    scope: str | None = None
