"""Context assembly and compaction (ADR-P08): the model's context as a pure projection.

Built so far: the declared policy, ``ContextPolicy`` and ``SummaryPolicy``, and
``CONTEXT_POLICY_VERSION``. Truncation, observation masking and a recorded summary are
designed; decision 34 makes compaction a must-have of the first milestone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

CONTEXT_POLICY_VERSION: Final[int] = 1
"""Bumped when stub, recitation or render text changes."""


@dataclass(frozen=True)
class SummaryPolicy:
    """Summary compaction (designed, not built)."""

    alias: str = "generate.summary"
    trigger_chars: int = 80_000
    keep_recent_pairs: int = 3
    max_per_instance: int = 2


@dataclass(frozen=True)
class ContextPolicy:
    """Caps and compaction limits for one agent or Task, in characters of canonical JSON."""

    result_cap_chars: int = 8_000
    read_cap_chars: int = 12_000
    read_block_cap_chars: int = 40_000
    context_limit_chars: int = 100_000
    masking: bool = False
    high_water_chars: int = 60_000
    low_water_chars: int = 45_000
    keep_recent_pairs: int = 3
    exempt_tools: frozenset[str] = frozenset()
    summary: SummaryPolicy | None = None
