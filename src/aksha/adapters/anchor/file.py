"""``FileAnchorSink``: anchors as JSON Lines in one file, for tests and a laptop.

One anchor per line, in canonical JSON, appended and flushed to disk with ``fsync``.
Publishing is idempotent on ``(store_id, seq)``, and a second hash for an anchored seq is
refused. A file is a sink in name only when it sits beside the store and the same user can
edit both (ADR-P01 H7); point it at another disk, a mounted audit share or a WORM mount,
and the anchors outlive a rewrite of the store.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from aksha.runtime.canonical import canonical_json
from aksha.runtime.storage.chain import Anchor
from aksha.runtime.storage.errors import StoreError


def _anchor(line: str, path: Path, number: int) -> Anchor:
    try:
        data: Any = json.loads(line)
        return Anchor(
            store_id=str(data["store_id"]),
            seq=int(data["seq"]),
            hash=str(data["hash"]),
            at=str(data["at"]),
            sink=str(data["sink"]),
            receipt=None if data.get("receipt") is None else str(data["receipt"]),
        )
    except (ValueError, KeyError, TypeError) as e:
        raise StoreError("anchor_file_invalid", f"{path} line {number}: {e}") from None


class FileAnchorSink:
    """An :class:`~aksha.runtime.storage.ports.AnchorSink` over one JSON Lines file."""

    def __init__(self, path: Path) -> None:
        """Keep anchors in ``path``; the file and its folder are created on first publish."""
        self._path = path

    @property
    def name(self) -> str:
        """``file:`` plus the file's path."""
        return f"file:{self._path}"

    def _all(self) -> list[Anchor]:
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        return [_anchor(line, self._path, i) for i, line in enumerate(lines, 1) if line.strip()]

    def publish(self, anchor: Anchor) -> Anchor:
        """Append ``anchor`` (with ``sink`` set to this sink) unless its seq is anchored."""
        stored = replace(anchor, sink=self.name)
        for existing in self._all():
            if (existing.store_id, existing.seq) == (anchor.store_id, anchor.seq):
                if existing.hash != anchor.hash:
                    raise StoreError(
                        "anchor_conflict",
                        f"{anchor.store_id} seq {anchor.seq} is anchored as {existing.hash},"
                        f" not {anchor.hash}",
                    )
                return existing
        self._path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "store_id": stored.store_id,
            "seq": stored.seq,
            "hash": stored.hash,
            "at": stored.at,
            "sink": stored.sink,
            "receipt": stored.receipt,
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(canonical_json(record) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return stored

    def read(self, store_id: str) -> tuple[Anchor, ...]:
        """Every anchor for ``store_id``, lowest seq first."""
        return tuple(
            sorted((a for a in self._all() if a.store_id == store_id), key=lambda a: a.seq)
        )
