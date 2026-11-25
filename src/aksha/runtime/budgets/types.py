"""Budget values: ``Backoff``, ``Budget`` and ``Limits`` (ADR-P14).

``Limits`` holds the turn, veto and restart limits; the restart limit counts resumes
since the last progress (ADR-P14 S2). ``Remaining`` and ``Grant`` arrive with the meter.
"""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Backoff:
    """Retry delays: ``base_ms`` times ``factor`` per attempt, capped. No jitter, for replay."""

    base_ms: int = 500
    factor: int = 2
    cap_ms: int = 4_000


@dataclass(frozen=True)
class Budget:
    """The caps of an agent, a Task, or the runtime default. ``None`` means not capped."""

    model_turns: int = 25
    tool_calls: int = 40
    active_ms: int = 120_000
    tokens: int | None = None
    cost_micro_usd: int | None = None

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
    """Turn, veto, restart, retry and timeout limits, each counted from the event store."""

    turn_limit: int = 10
    stop_veto_limit: int = 3
    restart_limit: int = 3
    auto_starts: tuple[int, int] = (3, 300_000)
    max_retries: int = 2
    tool_timeout_ms: int = 10_000
    model_timeout_ms: int = 60_000
    backoff: Backoff = Backoff()
