"""MailboxSystem: a mock mailbox ledgered in a JSON file, the offline stand-in for an inbox.

It implements the outbox's ``SystemOfRecord`` port (ADR-P03). It keeps one
row per idempotency key in ``<path>`` and writes each row with an atomic replace and an
``fsync``, so a row that landed survives the process dying right after. A key it already
holds returns its first receipt and delivers nothing again; ``lookup`` answers by key, so
recovery can ask whether a send landed instead of sending again. ``deliveries`` counts the
sends that really happened, one per row, which is what a reader checks for exactly once.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aksha.runtime.canonical import thaw
from aksha.runtime.outbox.types import SystemReceipt


class MailboxSystem:
    """A ledgered mock mailbox in one JSON file (module doc)."""

    def __init__(self, path: Path, name: str) -> None:
        """Keep the ledger of mailbox ``name`` in ``path``."""
        self.path = path
        self.name = name

    def ledger_id(self) -> str:
        """Which ledger answers."""
        return f"{self.name}-ledger-1"

    def rows(self) -> dict[str, Any]:
        """Every row by idempotency key, in the order they landed."""
        if not self.path.exists():
            return {}
        found: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        return found

    def apply(self, key: str, request: Mapping[str, Any]) -> SystemReceipt:
        """Deliver once: a new key adds a row; a known key returns its first receipt."""
        rows = self.rows()
        if key not in rows:
            rows[key] = {"message_id": f"m-{len(rows) + 1}", "request": thaw(request)}
            self._write(rows)
        return self._receipt(key, rows)

    def lookup(self, key: str, request: Mapping[str, Any]) -> SystemReceipt | None:
        """The receipt of the send under ``key``, or None when it never landed."""
        rows = self.rows()
        return self._receipt(key, rows) if key in rows else None

    def deliveries(self) -> int:
        """How many sends really happened: one per row."""
        return len(self.rows())

    def _receipt(self, key: str, rows: Mapping[str, Any]) -> SystemReceipt:
        return SystemReceipt(key, self.ledger_id(), {"message_id": rows[key]["message_id"]})

    def _write(self, rows: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, sort_keys=True, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
