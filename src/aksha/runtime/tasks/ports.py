"""Ports of the Task model (ADR-P15): ``KeyLister``, implemented outside the core.

A ``source_keys("notes")`` key source fans out over the records of a collection of a
versioned source. The core never reads the source itself: ``keys.list_source_keys`` asks
a ``KeyLister`` and records what it answered as a ``keys.listed`` fact, so the scheduler's
pick stays a pure function of the event store.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class KeyLister(Protocol):
    """Lists the record keys of one collection for one owner (``pages`` holds ``P-0001``)."""

    def keys(self, owner: str, collection: str) -> Sequence[str]:
        """The keys of ``collection`` for ``owner`` now, in any order."""
        ...
