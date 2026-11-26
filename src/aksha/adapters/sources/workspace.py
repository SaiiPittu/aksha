"""Each owner's input workspace: uploaded files as one versioned source (ADR-P05, ADR-P13 D1).

An agent that works on a person's own material (a folder of notes, a CSV export, saved web
pages, emails, text extracted from a PDF) reads it as the records of one source, ``inputs``
by default, in one collection, ``input``. ``InputWorkspace(name, root)`` keeps each
owner's uploads under ``root/<owner>/``: uploading ``notes/a.md`` writes
``root/<owner>/notes/a.md`` and the record is ``input/notes/a.md``, its version the
``sha256:`` of its bytes. Uploading the same path again replaces the file; a file that is
not uploaded again stays. So re-uploading one changed file moves exactly that record, and
the runtime's invalidation reopens only the work built on it (ADR-P05 D6, per owner).

``base`` is an optional configured folder (``AKSHA_SOURCES``) every owner also sees, as if
uploaded; an upload of the same path wins for that owner. A file added later reaches the
work that listed or searched the inputs through its recorded query (ADR-P05 D6). Upload
paths are relative, ``/``-separated, with no empty, ``.`` or ``..`` segment and no ``#``;
anything else is refused before any write.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from aksha.adapters.sources.folder import FolderSource
from aksha.runtime.grounding.parts import part_version
from aksha.runtime.grounding.types import ReturnedText

_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_FILE_BYTES = 8 * 1024 * 1024
"""The largest uploaded file, in bytes: inputs are documents, not datasets."""


class UploadRefused(ValueError):
    """An upload names a path or content the workspace does not take; nothing was written."""


def owner_folder(owner: str) -> str:
    """The folder name of ``owner``: the owner itself when it is a plain name, else a hash."""
    if _OWNER.match(owner):
        return owner
    return "o-" + hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]


def check_path(path: str) -> str:
    """``path`` when it is a clean relative upload path (module doc); raises otherwise."""
    parts = path.split("/")
    bad = (
        not path
        or path.startswith("/")
        or "\\" in path
        or "#" in path
        or "\0" in path
        or any(p in ("", ".", "..") for p in parts)
    )
    if bad:
        raise UploadRefused(f"{path!r} is not a relative path inside the inputs")
    return path


class OwnerInputs:
    """One owner's view of the workspace: its uploads over the configured base folder."""

    def __init__(self, name: str, collection: str, upload: FolderSource, base: FolderSource | None):
        """Read ``upload`` first, then ``base``."""
        self.name = name
        self.collection = collection
        self.upload = upload
        self.base = base

    def records(self, source: str, collection: str) -> tuple[str, ...]:
        """Every input's record ID when ``collection`` is the inputs collection."""
        if source != self.name or collection != self.collection:
            return ()
        found = set(self.upload.records(source, collection))
        if self.base is not None:
            found |= set(self.base.records(source, collection))
        return tuple(sorted(found))

    def serves(self, source: str) -> bool:
        """Whether ``source`` is the inputs source."""
        return source == self.name

    def read(self, source: str, record_id: str) -> ReturnedText | None:
        """The input's text (the upload's when both have it)."""
        if source != self.name:
            return None
        found = self.upload.read(source, record_id)
        if found is None and self.base is not None:
            found = self.base.read(source, record_id)
        return found


class InputWorkspace:
    """Every owner's uploaded inputs as one versioned source (module doc)."""

    def __init__(
        self, name: str, root: Path, *, collection: str = "input", base: Path | None = None
    ) -> None:
        """Keep uploads under ``root``; serve ``base`` to every owner as well."""
        self.name = name
        self.root = root
        self.collection = collection
        self.base = None if base is None else FolderSource(name, base, collection)

    @property
    def collections(self) -> tuple[str, ...]:
        """The one collection every input is in."""
        return (self.collection,)

    def folder(self, owner: str) -> Path:
        """Where ``owner``'s uploads are kept."""
        return self.root / owner_folder(owner)

    def reader(self, owner: str) -> OwnerInputs:
        """``owner``'s inputs as a record reader."""
        upload = FolderSource(self.name, self.folder(owner), self.collection)
        return OwnerInputs(self.name, self.collection, upload, self.base)

    def put(self, owner: str, files: Mapping[str, bytes]) -> tuple[str, ...]:
        """Write ``files`` (relative path to bytes) for ``owner``; the record IDs that changed.

        Every path and size is checked before anything is written. Each file is written to
        a temporary file outside every owner's folder (``root/.staging``, a name no owner
        folder can have) and renamed over the old one, so a reader never sees half a file
        and a crash mid-write leaves no stray record among the owner's inputs.
        """
        checked = {check_path(p): data for p, data in files.items()}
        for p, data in checked.items():
            if len(data) > MAX_FILE_BYTES:
                raise UploadRefused(f"{p}: larger than {MAX_FILE_BYTES} bytes")
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                raise UploadRefused(
                    f"{p}: not UTF-8 text, and no ingester is configured to convert it"
                ) from None
        folder = self.folder(owner)
        changed: list[str] = []
        for p, data in sorted(checked.items()):
            target = folder / p
            if target.is_file() and target.read_bytes() == data:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = self.root / ".staging"
            staging.mkdir(parents=True, exist_ok=True)
            tmp = staging / hashlib.sha256(f"{owner}\0{p}".encode()).hexdigest()
            tmp.write_bytes(data)
            os.replace(tmp, target)
            changed.append(f"{self.collection}/{p}")
        return tuple(changed)

    def parts_of(self, owner: str, parent: str) -> tuple[str, ...]:
        """The relative paths served under ``parent``'s attachment folder (``<parent>.parts/``)."""
        folder = self.folder(owner)
        parts = folder / (check_path(parent) + ".parts")
        if not parts.is_dir():
            return ()
        return tuple(sorted(f.relative_to(folder).as_posix() for f in parts.rglob("*")
                            if f.is_file()))  # fmt: skip

    def remove(self, owner: str, paths: Iterable[str]) -> tuple[str, ...]:
        """Delete ``paths`` from ``owner``'s inputs; the record IDs that went.

        Folders left empty under the owner's folder are removed too. A path not on disk is
        skipped. Like :meth:`put`, this only changes files: the caller records what moved.
        """
        folder = self.folder(owner)
        gone: list[str] = []
        for p in sorted({check_path(p) for p in paths}):
            target = folder / p
            if not target.is_file():
                continue
            target.unlink()
            gone.append(f"{self.collection}/{p}")
            up = target.parent
            while up != folder and up.is_dir() and not any(up.iterdir()):
                up.rmdir()
                up = up.parent
        return tuple(gone)

    def highest_named(self, owner: str, prefix: str) -> int:
        """The highest ``n`` of a ``<prefix>-<n>`` on the first line of any of ``owner``'s files.

        A converted input is served under a header that names its doc key, and the files
        are written before the upload's facts commit. A process that stops between the two
        leaves a key on disk the log has never seen; the next upload counts from past it,
        so one key never names two files. A key that only looks like one skips a number,
        which is harmless.
        """
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(prefix)}-([0-9]+)(?![0-9])")
        folder = self.folder(owner)
        best = 0
        if not folder.is_dir():
            return best
        for f in folder.rglob("*"):
            if not f.is_file():
                continue
            with f.open("rb") as fh:
                head = fh.readline(4096).decode("utf-8", "replace")
            for m in pattern.finditer(head):
                best = max(best, int(m.group(1)))
        return best

    def version(self, owner: str) -> str:
        """The owner's whole input set's version: a hash over every record and its version."""
        reader = self.reader(owner)
        h = hashlib.sha256()
        for r in reader.records(self.name, self.collection):
            found = reader.read(self.name, r)
            h.update(f"{r}\0{None if found is None else found.version}\n".encode())
        return "w-" + h.hexdigest()[:16]

    def record_version(self, owner: str, record_id: str) -> str | None:
        """The record's current version for ``owner``; a record part has its part's."""
        record, _, fragment = record_id.partition("#")
        found = self.reader(owner).read(self.name, record)
        if found is None:
            return None
        return part_version(found.text, fragment, found.version) if fragment else found.version
