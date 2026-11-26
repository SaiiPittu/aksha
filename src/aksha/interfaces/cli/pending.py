"""The CLI's pending command IDs (ADR-P19 D2): one random ID per invocation until its receipt.

A CLI command takes its command ID from here before it dispatches and clears it once a
receipt arrives. A command killed in between leaves its ID behind, so running the same
command line again (the retry) reuses that ID and gets the first command's receipt
instead of committing twice. Once the receipt arrived, the next identical command line is
a new invocation with a new ID. The file sits beside the store; it is a convenience for
retries, never a record: deleting it loses nothing the event store holds.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path

from aksha.interfaces.wire import new_command_id

PENDING_FILE = "aksha.pending.json"


def invocation_key(owner: str, argv: Sequence[str]) -> str:
    """The key of one command line for one owner."""
    return hashlib.sha256(json.dumps([owner, list(argv)]).encode("utf-8")).hexdigest()[:32]


class PendingIds:
    """Pending command IDs by invocation key, in one small JSON file."""

    def __init__(self, path: Path) -> None:
        """Keep the IDs in ``path``."""
        self.path = path

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _save(self, held: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(held, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def take(self, key: str, make: Callable[[], str] = new_command_id) -> str:
        """The pending value of ``key``, or a new one from ``make`` recorded as pending.

        The CLI keeps the command ID here, and a generated session ID beside it, so a
        retry names the same session as the command it repeats.
        """
        held = self._load()
        if key not in held:
            held[key] = make()
            self._save(held)
        return held[key]

    def clear(self, key: str) -> None:
        """Forget ``key``'s ID: its receipt arrived."""
        held = self._load()
        if held.pop(key, None) is not None:
            self._save(held)
