"""FolderSystem: a mock system of record that keeps records as files in one folder.

It implements the outbox's ``SystemOfRecord`` port (ADR-P03) for an approved source
correction (ADR-P05 C1): ``apply`` writes the request's ``text`` as the record's file,
``<folder>/<path>`` for a record ``<collection>/<path>``, so a versioned source over the
same folder sees the record's next version. A ledger kept outside that folder (a JSON
file written with an atomic replace and an ``fsync``) keeps one row
per idempotency key: a key it already holds returns its first receipt and writes nothing
again, and ``lookup`` answers by key. It is the offline stand-in for a shared system that
owns records, never a real one.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aksha.runtime.outbox.types import SystemReceipt


class FolderSystem:
    """Records as files in ``folder``, with a ledger of applied keys (module doc)."""

    def __init__(self, folder: Path, name: str, ledger: Path) -> None:
        """Write the records of system ``name`` into ``folder``; keep its ledger in ``ledger``."""
        self.folder = folder
        self.name = name
        self.ledger = ledger

    def ledger_id(self) -> str:
        """Which ledger answers."""
        return f"{self.name}-ledger-1"

    def rows(self) -> dict[str, Any]:
        """Every applied row by idempotency key."""
        path = self.ledger
        if not path.is_file():
            return {}
        found: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return found

    def apply(self, key: str, request: Mapping[str, Any]) -> SystemReceipt:
        """Write the record once: a new key writes the file and a row; a known key does not."""
        rows = self.rows()
        if key not in rows:
            path = self._path(str(request["record"]))
            data = str(request["text"]).encode("utf-8")
            path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic(path, data)
            version = "sha256:" + hashlib.sha256(data).hexdigest()
            rows[key] = {"record": str(request["record"]), "version": version}
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            self._atomic(self.ledger, json.dumps(rows, sort_keys=True).encode())
        return self._receipt(key, rows)

    def lookup(self, key: str, request: Mapping[str, Any]) -> SystemReceipt | None:
        """The receipt of the write under ``key``, or None when it never landed."""
        rows = self.rows()
        return self._receipt(key, rows) if key in rows else None

    def _path(self, record: str) -> Path:
        _, _, rel = record.partition("/")
        parts = rel.split("/")
        if not rel or any(p in ("", ".", "..") for p in parts) or "\\" in rel or "#" in rel:
            raise ValueError(f"{record!r} names no file inside the folder")
        return self.folder.joinpath(*parts)

    def _receipt(self, key: str, rows: Mapping[str, Any]) -> SystemReceipt:
        row = rows[key]
        return SystemReceipt(key, self.ledger_id(), {"record": row["record"],
                                                     "version": row["version"]})  # fmt: skip

    @staticmethod
    def _atomic(path: Path, data: bytes) -> None:
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
