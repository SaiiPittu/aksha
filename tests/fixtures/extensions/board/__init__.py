"""The board extension: a system of record the scribe's ``publish`` delivers to (ADR-P20 S2).

``BoardLedger`` keeps one row per idempotency key in a JSON file inside the folder the
composition root gives each contributed system (``state_dir``), so a row a CLI command
delivered is still there for the next command, and a repeated key returns its first
receipt. Synthetic: it stands for an outside board that indexes the key. It imports only
:mod:`aksha.runtime.api`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aksha.runtime.api import Extension, SystemOfRecordSpec, SystemReceipt

LEDGER_FILE = "rows.json"


class BoardLedger:
    """A file-backed ledger keyed by the idempotency key."""

    def __init__(self, state_dir: Path) -> None:
        """Keep the rows in ``state_dir/rows.json``."""
        self.path = state_dir / LEDGER_FILE

    def rows(self) -> dict[str, dict[str, Any]]:
        """Every delivered row, by key."""
        if not self.path.is_file():
            return {}
        found: dict[str, dict[str, Any]] = json.loads(self.path.read_text(encoding="utf-8"))
        return found

    def ledger_id(self) -> str:
        """The one board ledger."""
        return "board-ledger-1"

    def apply(self, key: str, request: Mapping[str, Any]) -> SystemReceipt:
        """Post once per key; a known key returns its first receipt."""
        rows = self.rows()
        if key not in rows:
            rows[key] = {"post_id": f"SYN-POST-{len(rows) + 1:04d}", **dict(request)}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(rows, sort_keys=True), encoding="utf-8")
        return self._receipt(key, rows)

    def lookup(self, key: str, request: Mapping[str, Any]) -> SystemReceipt | None:
        """The receipt of ``key`` when it landed."""
        rows = self.rows()
        return self._receipt(key, rows) if key in rows else None

    def _receipt(self, key: str, rows: Mapping[str, Mapping[str, Any]]) -> SystemReceipt:
        return SystemReceipt(key, self.ledger_id(), {"post_id": rows[key]["post_id"]})


def ledger(*, state_dir: Path) -> BoardLedger:
    """The factory the composition root calls with the system's state folder."""
    return BoardLedger(state_dir)


EXTENSION = Extension(
    name="board",
    version="0.1.0",
    requires_api=">=2.2,<4",
    systems=(SystemOfRecordSpec("ledger", ledger),),
)
