"""Decoded rows, reused while the stored row is unchanged.

Reading a log decodes every row: the payload's JSON, then a deep freeze. A runtime reads
the same owner's log many times per turn, so most of a read was decoding rows it had
already decoded. ``RowMemo`` keeps each decoded value with the row it came from and hands
it back only when the row just fetched is equal to that row, column for column. A row
changed behind the store's back (a tampered file) is decoded afresh, so a read never
returns anything the database does not hold now. It is a cache of a pure function of the
row, never a second copy of the facts: dropping it changes no answer.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any


class RowMemo[T]:
    """``decode(row)``, memoised by a key and checked against the whole row."""

    def __init__(self, decode: Callable[[tuple[Any, ...]], T]) -> None:
        """Memoise ``decode``, a pure function of one row."""
        self._decode = decode
        self._rows: dict[Hashable, tuple[tuple[Any, ...], T]] = {}

    def get(self, key: Hashable, row: tuple[Any, ...]) -> T:
        """The decoded ``row``; the kept value only if ``row`` equals the row it came from."""
        kept = self._rows.get(key)
        if kept is not None and kept[0] == row:
            return kept[1]
        value = self._decode(row)
        self._rows[key] = (row, value)
        return value

    def clear(self) -> None:
        """Forget every decoded row."""
        self._rows.clear()
