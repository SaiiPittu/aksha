"""``ViewArtifactReader``: the ``ArtifactReader`` a read tool is given (ADR-P09 D3, D5).

The agent loop builds one per call from the log as it stood before the step started
(``context.turns.as_of``), so a body reads the same versions on every attempt and every
replay. It answers only for the kinds the tool declares in ``reads_kinds``; every answer
carries the version's freshness in the invalidation view and whether its slot has a newer
version in any run. The invalidation view is computed once, on the first answer that
needs it.
"""

from __future__ import annotations

from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.types import ArtifactRef, ArtifactSnapshot, ArtifactVersion
from aksha.runtime.artifacts.versions import unaccepted_revisions, version, versions
from aksha.runtime.errors import AkshaError
from aksha.runtime.invalidation.view import InvalidationView, invalidation_view
from aksha.runtime.projections.view import LogView


class KindNotReadable(AkshaError):
    """A body asked for a kind its tool does not declare in ``reads_kinds``."""

    default_code = "kind_not_readable"


class ViewArtifactReader:
    """Versions of ``kinds`` (full names) in ``view``, read-only (module doc)."""

    def __init__(
        self, view: LogView, kinds: frozenset[str], blobs: BlobStore | None = None
    ) -> None:
        """Read ``view``, answering only for ``kinds``; offloaded content comes from ``blobs``."""
        self._view = view
        self._kinds = kinds
        self._blobs = blobs
        self._iv: InvalidationView | None = None
        self._pending = unaccepted_revisions(view)

    def latest(self, kind: str) -> tuple[ArtifactSnapshot, ...]:
        """The current version of every slot of ``kind``, sorted by key."""
        self._check(kind)
        keys = {
            e.payload.get("key")
            for e in self._view.events
            if e.type == "artifact.saved" and e.payload.get("kind") == kind
        }
        out: list[ArtifactSnapshot] = []
        for key in sorted(keys, key=lambda k: "" if k is None else str(k)):
            found = self._counted(kind, key)
            if found:
                out.append(self._snapshot(found[-1], latest=True))
        return tuple(out)

    def get(self, ref: str) -> ArtifactSnapshot | None:
        """The version ``ref`` names, or None."""
        parsed = ArtifactRef.parse(ref)
        self._check(parsed.kind)
        found = version(self._view, parsed, blobs=self._blobs)
        if found is None:
            return None
        newest = self._counted(parsed.kind, parsed.key)
        return self._snapshot(found, latest=bool(newest) and newest[-1].ref == parsed)

    def _counted(self, kind: str, key: str | None) -> list[ArtifactVersion]:
        """The slot's versions a reader sees, oldest first: no unaccepted revision."""
        return [
            v
            for v in versions(self._view, kind, key, blobs=self._blobs)
            if str(v.ref) not in self._pending
        ]

    def _snapshot(self, v: ArtifactVersion, *, latest: bool) -> ArtifactSnapshot:
        if self._iv is None:
            self._iv = invalidation_view(self._view)
        state = self._iv.versions.get(str(v.ref))
        freshness = "current" if state is None else state.freshness
        return ArtifactSnapshot(v.ref, v.content, v.writer, v.run, freshness, latest)

    def _check(self, kind: str) -> None:
        if kind not in self._kinds:
            raise KindNotReadable(message=f"{kind!r} is not in this tool's reads_kinds")
