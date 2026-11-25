"""The projection cache, keyed by (owner, reducer name, args, last sequence read).

It keeps one state per (owner, reducer name, args): the newest. A commit by an owner makes
that owner's earlier entries unreachable; a commit by another owner touches nothing. It
is a snapshot, never an authority (ADR-P01 D2): clearing it changes no answer.

The runtime owns one cache per process and passes it to every ``project`` call. Only a
complete view (``LogView.complete``, made by ``load_view``) is cached, because the key's
sequence number says nothing about a view built from a filtered read.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any

_Key = tuple[str, str, Hashable]


class ProjectionCache:
    """In-memory projection states, one per owner, reducer and arguments."""

    def __init__(self) -> None:
        """Start empty."""
        self._entries: dict[_Key, tuple[int, Any]] = {}

    def get(self, owner: str, name: str, args: Hashable, seq: int) -> tuple[bool, Any]:
        """``(True, state)`` for the state cached at exactly ``seq``, else ``(False, None)``.

        The flag, not the state, says whether it was found, so a reducer whose state is
        ``None`` (or any falsy value) still gets its hits.
        """
        entry = self._entries.get((owner, name, args))
        if entry is not None and entry[0] == seq:
            return True, entry[1]
        return False, None

    def latest_before(
        self, owner: str, name: str, args: Hashable, seq: int
    ) -> tuple[int, Any] | None:
        """The cached ``(seq, state)`` read before ``seq``, for the incremental path."""
        entry = self._entries.get((owner, name, args))
        return entry if entry is not None and entry[0] < seq else None

    def put(self, owner: str, name: str, args: Hashable, seq: int, state: Any) -> None:
        """Keep ``state`` as read at ``seq``, unless a newer state is already kept."""
        key = (owner, name, args)
        kept = self._entries.get(key)
        if kept is None or kept[0] <= seq:
            self._entries[key] = (seq, state)

    def clear(self) -> None:
        """Forget every state; the next ``project`` rebuilds."""
        self._entries.clear()

    def __len__(self) -> int:
        """How many states are kept."""
        return len(self._entries)
