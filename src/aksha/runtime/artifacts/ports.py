"""The ``BlobStore`` port: content-addressed bytes outside the event store (decision 25).

A blob is a byte string stored once under the SHA-256 of its bytes, for content too large
for an event payload (over 64 KiB) or too binary for JSON (ADR-P01 D7, ADR-P04 D10). The
rules every adapter keeps, checked by ``tests/contract/blobs``:

1. Content-addressed: ``put`` returns ``sha256:<hex>`` of exactly the bytes given, and the
   same bytes always give the same ref and one stored copy.
2. Write-once: a stored blob is never replaced or removed through the port; a put of
   bytes whose hash names a corrupt copy may only rewrite it with those same bytes.
3. Verified on read: ``get`` re-hashes what it read and refuses bytes that no longer match
   their name (``blob_corrupt``), so an edited blob is never handed out as the original.
4. Refuses a ref that is not ``sha256:`` and 64 lowercase hex characters
   (``blob_ref_invalid``) before touching storage, so a ref can never name a path.

The filesystem adapter is the default (laptop, offline, tests); an object-store adapter
for the software factory implements the same port and passes the same suite.
"""

from __future__ import annotations

from typing import Protocol


class BlobStore(Protocol):
    """Content-addressed, write-once byte storage."""

    def put(self, data: bytes) -> str:
        """Store ``data`` once and return its ref, ``sha256:<hex>``; idempotent."""
        ...

    def get(self, ref: str) -> bytes:
        """The bytes named ``ref``; ``BlobError`` ``blob_not_found`` or ``blob_corrupt``."""
        ...

    def has(self, ref: str) -> bool:
        """Whether a blob named ``ref`` is stored (its bytes are not re-hashed)."""
        ...
