"""A folder of files as one versioned source: the record reader and its record versions.

``FolderSource(name, root)`` serves the files under ``root`` as records of source
``name``: a record ID is the file's path relative to ``root`` (``pages/P-0001.html``), its
collection the first folder of that path, its text the file decoded as UTF-8 with nothing
changed (offsets count code points in the raw file), and its version ``sha256:`` over the
file's bytes, so an edited file is a new version. A record ID that leaves ``root`` (an
absolute path, ``..``, a symbolic link out) reads as no record. It implements the
runtime's ``RecordReader`` port and ``record_version`` of the ``VersionedSource`` port.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from aksha.runtime.grounding.types import ReturnedText


class FolderSource:
    """The files under ``root`` as the records of one source named ``name`` (module doc)."""

    def __init__(self, name: str, root: Path) -> None:
        """Serve ``root``'s files as source ``name``."""
        self.name = name
        self.root = root.resolve()

    def _path(self, record_id: str) -> Path | None:
        if not record_id or record_id.startswith("/") or "\\" in record_id:
            return None
        path = (self.root / record_id).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return None
        return path

    def read(self, source: str, record_id: str) -> ReturnedText | None:
        """The record's text at its version; None for another source or no such file."""
        path = self._path(record_id) if source == self.name else None
        if path is None:
            return None
        data = path.read_bytes()
        version = "sha256:" + hashlib.sha256(data).hexdigest()
        return ReturnedText(source, record_id, version, data.decode("utf-8"))

    def records(self, source: str, collection: str) -> tuple[str, ...]:
        """The record IDs under ``collection``'s folder, sorted; empty for another source."""
        if source != self.name or not collection or "/" in collection or collection == "..":
            return ()
        folder = self.root / collection
        if not folder.is_dir():
            return ()
        found = (p.relative_to(self.root).as_posix() for p in folder.rglob("*") if p.is_file())
        return tuple(sorted(r for r in found if self._path(r) is not None))

    def record_version(self, owner: str, record_id: str) -> str | None:
        """The record's current version (the same for every owner); None when it is gone."""
        found = self.read(self.name, record_id)
        return None if found is None else found.version
