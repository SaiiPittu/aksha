"""The event store's schema version, which every adapter writes once and checks on every open.

Each adapter keeps a one-row, append-only ``meta`` table holding ``schema_version``. It is
written in the same transaction that creates the tables, and read on every open, writer
or reader. A store whose tables have another shape (an older build, or no ``meta`` at
all) is refused with ``StoreError("schema_mismatch")`` rather than opened with columns
the code does not expect. A migration is a new version and a reviewed change, never a
silent ``CREATE TABLE IF NOT EXISTS``.
"""

from __future__ import annotations

from aksha.runtime.storage.errors import StoreError

SCHEMA_VERSION = 2
"""The shape of the ``events``, ``artifacts``, ``boots``, ``meta`` and ``store_meta`` tables.

Version 2 (25 Nov 2026) is the hash chain (ADR-P01 Amendment H): ``prev_hash`` and
``hash`` on every event, ``seq`` assigned by the writer, and the one-row ``store_meta``.
"""


def check_schema_version(found: int | None) -> None:
    """Refuse a store whose recorded version is not :data:`SCHEMA_VERSION`.

    ``found`` is None when the store has tables but no ``meta`` row, which means it was
    created before schema versions were recorded.
    """
    if found == SCHEMA_VERSION:
        return
    have = "no recorded schema version" if found is None else f"schema version {found}"
    raise StoreError(
        "schema_mismatch", f"the event store has {have}; this code needs {SCHEMA_VERSION}"
    )
