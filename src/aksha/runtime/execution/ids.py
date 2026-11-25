"""``LogIdSource``: IDs counted from the event store, per owner and prefix (ADR-P02 D3).

The next ID is one more than the highest ``<prefix>-<n>`` the owner's events mention, in
the envelope's ``run`` or any top-level payload string. Nothing is kept only in memory
across a restart, so a restarted process continues the sequence. Within one process an
ID handed out but not yet written is remembered, so two calls before an append differ;
an ID allocated by a transaction that never committed is allocated again after a
restart, which is safe because nothing outside the store saw it. IDs are per owner:
owner A's first run is ``R-0001``, and so is owner B's.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from aksha.runtime.events.envelope import Event
from aksha.runtime.storage.ports import EventStore

_PREFIX = re.compile(r"^[A-Z]{1,8}$")


def highest(events: Iterable[Event], prefix: str) -> int:
    """The highest ``n`` of ``<prefix>-<n>`` in the events' ``run`` or top-level payload strings."""
    pattern = re.compile(rf"^{re.escape(prefix)}-([0-9]+)$")
    best = 0
    for event in events:
        candidates = [event.run, *(v for v in event.payload.values() if isinstance(v, str))]
        for text in candidates:
            m = pattern.match(text) if text else None
            if m:
                best = max(best, int(m.group(1)))
    return best


class LogIdSource:
    """The :class:`~aksha.runtime.execution.ports.IdSource` that reads the event store."""

    def __init__(self, store: EventStore) -> None:
        """Count from ``store``."""
        self._store = store
        self._issued: dict[tuple[str, str], int] = {}

    def next(self, owner: str, prefix: str) -> str:
        """``<prefix>-<n:04d>``, ``n`` one more than any recorded or already issued."""
        if not _PREFIX.match(prefix):
            raise ValueError(f"an ID prefix is one to eight capital letters, not {prefix!r}")
        recorded = highest(self._store.read(owner), prefix)
        n = max(recorded, self._issued.get((owner, prefix), 0)) + 1
        self._issued[(owner, prefix)] = n
        return f"{prefix}-{n:04d}"
