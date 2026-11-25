"""Reading versions back from a ``LogView``: ``versions``, ``latest``, ``current``, ``version``.

A version is the pair of its ``artifact.saved`` event and its artifact row, which commit in
one transaction (ADR-P01 H6). Both are read from the view, never from a store handle, so a
reader sees exactly the versions committed by the view's ``seq_read``. A slot's versions
are numbered from 1 with no gaps, because each save takes the latest version plus one
under the single writer.
"""

from __future__ import annotations

from collections.abc import Iterator

from aksha.runtime.artifacts.blobs import inflate
from aksha.runtime.artifacts.hashing import reads_hash
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.types import ArtifactRef, ArtifactVersion, InputRef, WriterId
from aksha.runtime.events.envelope import Event
from aksha.runtime.projections.view import LogView


def _saved(view: LogView) -> Iterator[Event]:
    return (e for e in view.events if e.type == "artifact.saved")


def _version(view: LogView, event: Event, blobs: BlobStore | None) -> ArtifactVersion | None:
    p = inflate(event.payload, blobs)
    row = view.artifacts.get(str(p["ref"]))
    if row is None:
        return None
    reads = tuple(InputRef.from_json(r) for r in p.get("reads", ()))
    writer = p.get("writer")
    return ArtifactVersion(
        owner=view.owner,
        ref=ArtifactRef(str(p["kind"]), p["key"], int(p["version"])),
        content=row.row.content,
        content_hash=row.row.content_hash,
        canonical_hash=str(p.get("canonical_hash", row.row.canonical_hash)),
        reads=reads,
        reads_hash=str(p.get("reads_hash", reads_hash(reads))),
        writer=WriterId("", "") if writer is None else WriterId.from_json(writer),
        origin=p.get("origin", "task"),
        run=event.run,
        step=event.step,
        event_seq=event.seq,
        primary_inputs=tuple(InputRef.from_json(r) for r in p.get("primary_inputs", ())),
    )


def all_versions(view: LogView, *, blobs: BlobStore | None = None) -> tuple[ArtifactVersion, ...]:
    """Every version in the view, in commit order."""
    found = (_version(view, e, blobs) for e in _saved(view))
    return tuple(v for v in found if v is not None)


def versions(
    view: LogView, kind: str, key: str | None, *, blobs: BlobStore | None = None
) -> tuple[ArtifactVersion, ...]:
    """The slot's versions, oldest first."""
    found = (
        _version(view, e, blobs)
        for e in _saved(view)
        if e.payload.get("kind") == kind and e.payload.get("key") == key
    )
    return tuple(sorted((v for v in found if v is not None), key=lambda v: v.ref.version))


def latest(
    view: LogView, kind: str, key: str | None, *, blobs: BlobStore | None = None
) -> ArtifactVersion | None:
    """The slot's newest version, or None for an empty slot."""
    found = versions(view, kind, key, blobs=blobs)
    return found[-1] if found else None


def current(
    view: LogView, kind: str, key: str | None, *, blobs: BlobStore | None = None
) -> ArtifactVersion | None:
    """The latest version that is not an unaccepted pending revision.

    No pending revision exists until diff review (ADR-P05) is built, so this is
    :func:`latest`; callers that mean "current" say so, and keep their meaning when it is.
    """
    return latest(view, kind, key, blobs=blobs)


def version(
    view: LogView, ref: ArtifactRef, *, blobs: BlobStore | None = None
) -> ArtifactVersion | None:
    """The version ``ref`` names, if the view holds it."""
    text = str(ref)
    for e in _saved(view):
        if e.payload.get("ref") == text:
            return _version(view, e, blobs)
    return None
