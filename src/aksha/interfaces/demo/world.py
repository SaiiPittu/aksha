"""The demo's source folder: a copy of the base world, and the change set applied to it.

The change is the one ``stillroom-v2`` records against ``stillroom`` in its answer key
(``changes_from_base``), applied to the base world's own files, so nothing else moves:
v2's renames of people and companies are not part of it.

1. Added: ``publications/SYN-PUB-0011.json``, family E, which discloses C1's two gates.
2. Changed: the consultant's email ``Date`` header, 2026-01-05 to 2025-11-21.
3. Changed: the same date on row ``DISC-05`` of ``disclosures/history.csv``.

One more edit is the demo's own, outside the key (``UNCITED_EDIT``): one customer review,
in the export both agents read, that no output cites. It shows citation keying (ADR-P05
amendment M2): the outputs that read the export are noticed, not rebuilt.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aksha import data

WORLDS = data.worlds()
BASE = "stillroom"
VARIANT = "stillroom-v2"
ADDED = ("publications/SYN-PUB-0011.json",)
EDITS = (
    (
        "disclosures/consultant-email.eml.txt",
        "Date: Mon, 05 Jan 2026 16:42:10 +0100",
        "Date: Fri, 21 Nov 2025 16:42:10 +0100",
    ),
    ("disclosures/history.csv", "DISC-05,2026-01-05,", "DISC-05,2025-11-21,"),
)
"""Each changed file, the base text and its replacement; each must occur exactly once."""
UNCITED_EDIT = (
    "reviews/reviews.json",
    '"Best accuracy we tested."',
    '"Best accuracy we have tested."',
)
"""The demo's own edit: one customer review that no output cites, reworded."""


@dataclass(frozen=True)
class Change:
    """What the change set did to the folder, for the summary."""

    added: tuple[str, ...]
    edited: tuple[tuple[str, str, str], ...]
    uncited: tuple[str, str, str] = UNCITED_EDIT


def copy_base(work: Path) -> Path:
    """Copy the base world's ``sources/`` into ``work`` and return the copy."""
    target = work / "sources"
    shutil.copytree(WORLDS / BASE / "sources", target)
    return target


def apply_change(sources: Path) -> Change:
    """Apply the variant's change set to ``sources`` in place (module doc)."""
    for rel in ADDED:
        shutil.copyfile(WORLDS / VARIANT / "sources" / rel, sources / rel)
    for rel, old, new in (*EDITS, UNCITED_EDIT):
        path = sources / rel
        data = path.read_bytes()  # bytes, so line endings and offsets stay as they are
        if data.count(old.encode()) != 1:
            raise ValueError(f"{rel}: expected {old!r} exactly once")
        path.write_bytes(data.replace(old.encode(), new.encode()))
    return Change(ADDED, EDITS)


def variant_key() -> dict[str, Any]:
    """The variant's answer key; the demo compares its result with it, no tool reads it."""
    found: dict[str, Any] = json.loads((WORLDS / VARIANT / "key.json").read_text("utf-8"))
    return found
