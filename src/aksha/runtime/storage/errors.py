"""Storage refusals: ``StoreError`` and its named cases.

``AlreadyRecorded`` carries the existing event, so a caller that reached the same fact
twice reads the first one and moves on. ``LockHeld`` carries the holder's pid and exit
code 8. Every adapter raises these, never its driver's own exceptions.

Small refusals use ``StoreError`` with a code: ``owner_required``, ``empty_boundary``,
``system_owner_reserved``, ``store_closed``, ``store_missing``, ``store_unreachable``,
``driver_missing``, ``schema_mismatch`` (``storage/schema.py``), ``nul_in_field`` and
``nul_in_content``, ``not_canonical_field``, ``not_canonical_content``,
``content_hash_invalid``, ``content_hash_mismatch`` and ``artifact_row_missing``
(``storage/append.py``), ``constraint_violated``, ``backend_error``,
``encoding_unsupported`` (the PostgreSQL adapter), and for the hash chain
``chain_head_moved``, ``store_id_mismatch``, ``store_id_invalid``,
``chain_version_unsupported``, ``anchor_store_mismatch``, ``anchor_invalid``
(``storage/chain.py``), ``anchor_conflict`` and ``anchor_file_invalid`` (the file
anchor sink).
"""

from __future__ import annotations

from aksha.runtime.errors import AkshaError
from aksha.runtime.events.envelope import Event


class StoreError(AkshaError):
    """Any refusal by an event store."""

    default_code = "store_error"


class AlreadyRecorded(StoreError):
    """A dedup key already exists for this owner; nothing in the boundary was committed."""

    default_code = "already_recorded"

    def __init__(self, existing: Event) -> None:
        """Carry the event that already holds the key."""
        self.existing = existing
        super().__init__(message=f"{existing.dedup_key} is already recorded at seq {existing.seq}")


class LockHeld(StoreError):
    """Another process is the writer. The CLI exits with ``exit_code`` 8."""

    default_code = "lock_held"
    exit_code = 8

    def __init__(self, pid: int | None) -> None:
        """Name the holder's process ID, when the backend can tell."""
        self.pid = pid
        holder = f"process {pid}" if pid is not None else "another process"
        super().__init__(message=f"the event store's writer lock is held by {holder}")


class NotWriter(StoreError):
    """``append`` on a store opened read-only."""

    default_code = "not_writer"

    def __init__(self) -> None:
        """Explain that only the writer appends."""
        super().__init__(message="this store was opened read-only; only the writer appends")


class SequenceConflict(StoreError):
    """``append(expected_seq=...)`` found the owner's last seq moved; nothing was committed."""

    default_code = "sequence_conflict"

    def __init__(self, owner: str, expected: int, actual: int) -> None:
        """Name the owner, the seq the caller read and the seq the store holds."""
        self.owner = owner
        self.expected = expected
        self.actual = actual
        super().__init__(message=f"{owner}: expected last seq {expected}, store has {actual}")


class ArtifactWithoutEvent(StoreError):
    """An artifact row has no ``artifact.saved`` event for its ref in the same boundary."""

    default_code = "artifact_without_event"

    def __init__(self, ref: str) -> None:
        """Name the artifact ref that has no saved event."""
        self.ref = ref
        super().__init__(message=f"artifact {ref} has no artifact.saved event in its boundary")
