"""The grounding port: the reader a tool body reads versioned records through (ADR-P13 D1).

A body that mints citation spans asks the runtime's reader for a record's text; the
runtime keeps every text the reader returned in that attempt, and a span draft is minted
only over one of them. The composition root picks the adapter (a folder of files today).
"""

from __future__ import annotations

from typing import Protocol

from aksha.runtime.grounding.types import ReturnedText


class RecordReader(Protocol):
    """Reads one versioned source's records: their texts at a version, and their IDs."""

    def read(self, source: str, record_id: str) -> ReturnedText | None:
        """The record's canonical text at its current version; None when there is none."""
        ...

    def records(self, source: str, collection: str) -> tuple[str, ...]:
        """The record IDs of ``collection`` (``pages/P-0001``, sorted); empty when none."""
        ...

    def serves(self, source: str) -> bool:
        """Whether this reader serves ``source`` at all.

        ``read`` answers None both for a record a served source does not hold and for any
        record of a source it does not serve; this tells the two apart, so a check that
        must fail closed (the edit-time check, decision 38) never reads an unserved source
        as an empty one.
        """
        ...
