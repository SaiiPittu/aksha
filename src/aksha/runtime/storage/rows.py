"""Row shapes the event store persists beside events: ``ArtifactRow`` and ``StoredArtifact``.

An artifact row commits in the same transaction as its ``artifact.saved`` event, so an
artifact never exists without the fact that saved it (ADR-P01, ADR-P04). ADR-P04 owns
the columns beyond these and adds them when the artifact store is built.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aksha.runtime.canonical import freeze


@dataclass(frozen=True)
class ArtifactRow:
    """One immutable artifact version, as the artifact store hands it to ``append``."""

    ref: str
    kind: str
    key: str | None
    version: int
    content: Mapping[str, Any]
    content_hash: str
    canonical_hash: str

    def __post_init__(self) -> None:
        """Deep-freeze ``content`` so a stored version cannot be changed in memory either."""
        object.__setattr__(self, "content", freeze(self.content))


@dataclass(frozen=True)
class StoredArtifact:
    """An artifact row as read back: its owner and the seq of its ``artifact.saved`` event."""

    owner: str
    event_seq: int
    row: ArtifactRow

    @property
    def ref(self) -> str:
        """The artifact's ref, ``kind/key@vN``."""
        return self.row.ref
