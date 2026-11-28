"""The memory extension (ADR-P09): memory as artifacts, one recall tool, citations required.

An agent turns memory on by declaring a ``MemorySpec`` and applying
``memory_extension(spec, agent).apply`` to its definition; an agent that declares none is
untouched and hashes the same (D10). The parts:

1. ``kinds.py``: ``memory.preference``, ``memory.fact`` and ``memory.procedure``, keyed by
   subject, each with its citation rule as declared data (D2).
2. ``tools.py``: ``remember`` and ``forget`` return save requests (``forget`` a tombstone,
   D9) with ``supersedes`` set when a version exists (D4); ``recall`` reads current records
   through the context's artifact reader, skips facts whose cited version moved (D5),
   flags instruction-like sources (D7), and scores, sorts and caps the rest (D8).
3. ``extension.py``: ``apply`` puts recall first in each intake Task and refuses memory
   writes anywhere else (D3).
4. ``episodes.py``: past runs as a projection over the log (D6).
5. ``erasure.py``: ``used_by``, the lineage closure from a record, and
   ``erasure_inventory``, the hook ADR-P30 will act on; ``forget`` hides, never erases.

Memory content is data. The policy gate never reads an artifact, so no record can change
a tool, an approval, a recipient or a budget (D7). The extension imports only
:mod:`aksha.runtime.api` and itself, and the runtime core never imports it (D1).
"""

from __future__ import annotations

from aksha.runtime.api import Extension

from .episodes import episodes
from .erasure import ErasureInventory, erasure_inventory, used_by
from .extension import MemoryExtension, memory_extension
from .kinds import KINDS
from .types import (
    Episode,
    MemoryKind,
    MemoryRefused,
    MemorySpec,
    RecallHit,
    RecallResult,
    Veracity,
)

EXTENSION = Extension(name="memory", version="0.1.0", requires_api=">=2.8,<4", artifacts=KINDS)

__all__ = [
    "EXTENSION",
    "Episode",
    "ErasureInventory",
    "MemoryExtension",
    "MemoryKind",
    "MemoryRefused",
    "MemorySpec",
    "RecallHit",
    "RecallResult",
    "Veracity",
    "episodes",
    "erasure_inventory",
    "memory_extension",
    "used_by",
]
