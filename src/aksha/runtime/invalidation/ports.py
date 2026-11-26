"""Ports for invalidation (ADR-P05): ``VersionedSource`` and ``QueryRunner``.

Both are implemented outside the core: by the fixture world in tests and the offline run,
by a live search adapter or a repository remote later. The core only asks them for versions and
result-set hashes, and records what they answered as ``source.changed`` and
``query.reexecuted`` facts (S2).
"""

from __future__ import annotations

from typing import Protocol


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


class QueryRunner(Protocol):
    """Re-executes a recorded query for an owner and returns its result-set hash.

    S6: the hash covers the set of the hits' stable identities (``ToolSpec.result_key``),
    so the same hits in a new order hash the same.
    """

    def __call__(self, owner: str, query: str, /) -> str:
        """The result-set hash of ``query`` for ``owner`` now."""
        ...
