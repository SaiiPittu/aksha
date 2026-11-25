"""``FilesystemBlobStore``: blobs as read-only files named by their hash, under one folder.

Layout: ``<root>/sha256/<first two hex>/<all 64 hex>``, the fan-out a content-addressed
object store uses so no folder grows past a few thousand entries. A put writes a
temporary file in the target folder, flushes and syncs it, renames it into place (atomic
on one filesystem) and syncs the folder, so a crash leaves either the whole blob or none.
The file is made read-only. A blob already present is not written again unless its bytes
no longer hash to its name, in which case the put repairs it (the bytes given are, by
their hash, the right ones).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from pathlib import Path

from aksha.runtime.artifacts.blobs import check_blob_ref
from aksha.runtime.artifacts.errors import BlobError


class FilesystemBlobStore:
    """A :class:`~aksha.runtime.artifacts.ports.BlobStore` in a local folder."""

    def __init__(self, root: Path) -> None:
        """Keep blobs under ``root``, created if missing."""
        self._root = Path(root)
        (self._root / "sha256").mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """The folder the blobs live in."""
        return self._root

    def path_of(self, ref: str) -> Path:
        """The file that holds ``ref`` (refused first if ``ref`` is not a blob ref)."""
        digest = check_blob_ref(ref).split(":", 1)[1]
        return self._root / "sha256" / digest[:2] / digest

    def put(self, data: bytes) -> str:
        """Store ``data`` once and return ``sha256:<hex>``; idempotent."""
        ref = "sha256:" + hashlib.sha256(data).hexdigest()
        target = self.path_of(ref)
        if target.exists() and self._intact(target, ref):
            return ref
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o444)
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        _sync_dir(target.parent)
        return ref

    def get(self, ref: str) -> bytes:
        """The bytes of ``ref``, re-hashed: ``blob_not_found`` or ``blob_corrupt`` otherwise."""
        target = self.path_of(ref)
        try:
            data = target.read_bytes()
        except FileNotFoundError:
            raise BlobError("blob_not_found", f"no blob {ref}") from None
        if "sha256:" + hashlib.sha256(data).hexdigest() != ref:
            raise BlobError("blob_corrupt", f"{ref}: the stored bytes hash to something else")
        return data

    def has(self, ref: str) -> bool:
        """Whether a file for ``ref`` exists."""
        return self.path_of(ref).is_file()

    def _intact(self, target: Path, ref: str) -> bool:
        return "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest() == ref


def _sync_dir(folder: Path) -> None:
    """Sync ``folder`` so the rename survives a power cut (a no-op where unsupported)."""
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
