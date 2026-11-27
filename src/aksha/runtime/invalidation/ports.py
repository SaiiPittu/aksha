"""Ports for invalidation (ADR-P05): ``VersionedSource`` and ``QueryRunner``.

Both are implemented outside the core: by the fixture world in tests and the offline run,
by a live search adapter or a repository remote later. The core only asks them for versions and
result-set hashes, and records what they answered as ``source.changed`` and
``query.reexecuted`` facts (S2).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol, runtime_checkable

from aksha.runtime.grounding.ports import RecordReader
from aksha.runtime.grounding.types import ReturnedText


class VersionedSource(Protocol):
    """A source whose records have versions, per owner (D6: source changes are per owner).

    ``collections`` are the record collections it serves (``pages`` for ``pages/P-0001``).
    """

    @property
    def name(self) -> str:
        """The source's declared name."""
        ...

    @property
    def collections(self) -> tuple[str, ...]:
        """The record collections it serves."""
        ...

    def version(self, owner: str) -> str:
        """The owner's current version of the whole source."""
        ...

    def record_version(self, owner: str, record_id: str) -> str | None:
        """The record's current version for the owner; None when the record is gone."""
        ...


@runtime_checkable
class RecordTexts(Protocol):
    """A versioned source that can also hand back a record's current text (ADR-P05 M2).

    ``record_source_changes`` compares every span minted over a moved record with the
    record's new text through it (``excerpts.rechecked``), so citation keying can tell a
    changed cited span from a change elsewhere in the record. A source without it has its
    spans taken as changed.
    """

    def read(self, source: str, record_id: str) -> ReturnedText | None:
        """The record's text at its current version; None when there is none."""
        ...


@runtime_checkable
class PartialSource(Protocol):
    """A versioned source that answers only some records of its collections (ADR-P05 M2).

    The ``definitions`` source answers the definitions of the agents this process loaded;
    a recorded read of another agent's definition is one it cannot answer, not one that is
    gone, so ``record_source_changes`` skips a record the source does not cover.
    """

    def covers(self, record_id: str) -> bool:
        """Whether this source can answer ``record_id``'s current version."""
        ...


class QueryRunner(Protocol):
    """Re-executes a recorded query for an owner and returns its result-set hash.

    S6: the hash covers the set of the hits' stable identities (``ToolSpec.result_key``),
    so the same hits in a new order hash the same.
    """

    def __call__(self, owner: str, query: str, /) -> str:
        """The result-set hash of ``query`` for ``owner`` now."""
        ...


class InputWorkspace(Protocol):
    """Each owner's uploaded inputs: a versioned source that uploads write (ADR-P05 D6).

    ``put`` writes files for one owner and returns the record IDs whose bytes changed;
    ``remove`` deletes them (an uploaded file's attachment parts it no longer has);
    ``reader`` is that owner's record reader over them. The session records what an upload
    moved with the same source-change fact as any other change, so only the work built on
    a changed record reopens.
    """

    @property
    def name(self) -> str:
        """The source's declared name."""
        ...

    @property
    def collections(self) -> tuple[str, ...]:
        """The record collections it serves."""
        ...

    def version(self, owner: str) -> str:
        """The owner's current version of the whole source."""
        ...

    def record_version(self, owner: str, record_id: str) -> str | None:
        """The record's current version for the owner; None when the record is gone."""
        ...

    def put(self, owner: str, files: Mapping[str, bytes]) -> tuple[str, ...]:
        """Write ``files`` (relative path to bytes) for ``owner``; the record IDs that changed."""
        ...

    def parts_of(self, owner: str, parent: str) -> tuple[str, ...]:
        """The relative paths served under ``parent``'s attachment folder (``<parent>.parts/``)."""
        ...

    def remove(self, owner: str, paths: Iterable[str]) -> tuple[str, ...]:
        """Delete ``paths`` for ``owner``; the record IDs that went."""
        ...

    def highest_named(self, owner: str, prefix: str) -> int:
        """The highest ``n`` of a ``<prefix>-<n>`` on the first line of any of ``owner``'s files."""
        ...

    def reader(self, owner: str) -> RecordReader:
        """``owner``'s inputs as a record reader."""
        ...

    def uploaded(self, owner: str, record_id: str) -> bool:
        """Whether ``record_id`` is one of ``owner``'s own uploads (not the shared base)."""
        ...
