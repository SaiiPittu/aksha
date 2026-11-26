"""A folder of files as one versioned source: the record reader and its record versions.

``FolderSource(name, root)`` serves the files under ``root`` as records of source
``name``: a record ID is the file's path relative to ``root`` (``pages/P-0001.html``), its
collection the first folder of that path, its text the file decoded as UTF-8 with nothing
changed (offsets count code points in the raw file), and its version ``sha256:`` over the
file's bytes, so an edited file is a new version; a record part's version is that part's
(:mod:`aksha.runtime.grounding.parts`). A record ID is canonical or it is no
record: an absolute path, a backslash, an empty, ``.`` or ``..`` segment, or a symbolic
link out of ``root`` reads as none, so one file has exactly one record ID. It implements the
runtime's ``RecordReader`` port and the ``VersionedSource`` port.

``FolderSource(name, root, collection="input")`` serves the whole folder as that one
collection instead: ``input/notes/a.md`` is ``root/notes/a.md``. That is how a folder of
uploaded or configured inputs reaches an agent that discovers its inputs rather than
expecting fixed collections.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from aksha.runtime.grounding.parts import part_version
from aksha.runtime.grounding.types import ReturnedText


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FolderSource:
    """The files under ``root`` as the records of one source named ``name`` (module doc)."""

    def __init__(self, name: str, root: Path, collection: str | None = None) -> None:
        """Serve ``root``'s files as source ``name``, as one ``collection`` when given."""
        self.name = name
        self.root = root.resolve()
        self.collection = collection

    @property
    def collections(self) -> tuple[str, ...]:
        """The collections served: the one given, else every top folder of ``root``."""
        if self.collection is not None:
            return (self.collection,)
        if not self.root.is_dir():
            return ()
        return tuple(sorted(p.name for p in self.root.iterdir() if p.is_dir()))

    def _path(self, record_id: str) -> Path | None:
        if not record_id or record_id.startswith("/") or "\\" in record_id:
            return None
        rel = record_id
        if self.collection is not None:
            head, sep, rel = record_id.partition("/")
            if head != self.collection or not sep:
                return None
        if any(part in ("", ".", "..") for part in rel.split("/")):
            return None
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return None
        return path

    def _all(self) -> tuple[str, ...]:
        if not self.root.is_dir():
            return ()
        rels = (p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
        if self.collection is None:
            return tuple(sorted(r for r in rels if self._path(r) is not None))
        ids = (f"{self.collection}/{r}" for r in rels)
        return tuple(sorted(r for r in ids if self._path(r) is not None))

    def read(self, source: str, record_id: str) -> ReturnedText | None:
        """The record's text at its version; None for another source or no such file."""
        if source != self.name:
            return None
        path = self._path(record_id)
        if path is None:
            return None
        data = path.read_bytes()
        return ReturnedText(source, record_id, _sha(data), data.decode("utf-8"))

    def records(self, source: str, collection: str) -> tuple[str, ...]:
        """The record IDs under ``collection``, sorted; empty for another source."""
        if source != self.name or not collection or "/" in collection or collection == "..":
            return ()
        if self.collection is not None:
            return self._all() if collection == self.collection else ()
        folder = self.root / collection
        if not folder.is_dir():
            return ()
        found = (p.relative_to(self.root).as_posix() for p in folder.rglob("*") if p.is_file())
        return tuple(sorted(r for r in found if self._path(r) is not None))

    def record_version(self, owner: str, record_id: str) -> str | None:
        """The record's current version (the same for every owner); None when it is gone.

        A record part (``<record>#<selector>``) has its part's version (``part_version``).
        """
        record, _, fragment = record_id.partition("#")
        found = self.read(self.name, record)
        if found is None:
            return None
        return part_version(found.text, fragment, found.version) if fragment else found.version

    def version(self, owner: str) -> str:
        """The whole source's version: a hash over every record ID and its version."""
        h = hashlib.sha256()
        for c in self.collections:
            for r in self.records(self.name, c):
                h.update(f"{r}\0{self.record_version(owner, r)}\n".encode())
        return "w-" + h.hexdigest()[:16]


class FolderSources:
    """Several folder sources behind one record reader, each answering for its own name.

    ``FolderSources({"corpus": Path("sources")})`` serves source ``corpus`` from that
    folder; a source it does not hold reads as none, as an unconfigured reader would.
    ``FolderSources.of(sources)`` holds sources already built (one collection or many).
    """

    def __init__(self, folders: Mapping[str, Path]) -> None:
        """One :class:`FolderSource` per name in ``folders``."""
        self.sources = {name: FolderSource(name, root) for name, root in folders.items()}

    @classmethod
    def of(cls, sources: Mapping[str, FolderSource]) -> FolderSources:
        """A reader over ``sources`` as given."""
        out = cls({})
        out.sources = dict(sources)
        return out

    def read(self, source: str, record_id: str) -> ReturnedText | None:
        """The record's text from the folder of ``source``; None when it has none."""
        held = self.sources.get(source)
        return None if held is None else held.read(source, record_id)

    def records(self, source: str, collection: str) -> tuple[str, ...]:
        """The record IDs of ``collection`` in ``source``'s folder; empty when it has none."""
        held = self.sources.get(source)
        return () if held is None else held.records(source, collection)
