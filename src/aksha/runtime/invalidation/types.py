"""Invalidation values (ADR-P05): sources, read states, version states, plan entries, reviews.

``VersionedSourceSpec`` is what an agent or extension declares. The rest are what the
invalidation view, the rebuild plan and diff reviews hand to their callers; each is a
frozen value computed from the log, never stored (D3, pattern P-2).

The build-system names (decision 14, P05-1): a version is ``invalidated`` when one of its
direct inputs really changed, ``dirty`` when it is reachable from an invalidated version
through lineage that has not been rebuilt yet, and ``current_by_cutoff`` when an input was
rebuilt with an equal canonical hash (early cutoff).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from aksha.runtime.artifacts.types import ArtifactRef, InputRef, WriterId

Freshness = Literal["current", "invalidated", "dirty", "current_by_cutoff"]
"""A version's state in the invalidation view (D4)."""

ReadState = Literal["same", "equivalent", "moved", "unsettled", "gone"]
"""One read's state (D4): unchanged, rebuilt equal, changed, waiting on a rebuild, or gone."""

RebuildMode = Literal["regenerate", "propose", "copy_choice", "rebase"]
"""How a writer rebuilds an invalidated version (D7; ``rebase`` is S5 and not built)."""

SourceCause = Literal["command", "recovery", "clock"]
"""Who recorded a source change (S2): the session command, recovery's check, the clock."""

CLOCK_SOURCE = "time"
"""The source the runtime records the current date as (ADR-P25 D4); its version is the date."""

CLOCK_RECORD = "time/clock"
"""The record ``source.changed`` keeps the date under; a ``time:clock`` read is checked on it."""

EXPIRED = "expired"
"""What a read moved to when its freshness lifetime ran out (ADR-P25 D3, ``source.expired``)."""

STALE_STATES: frozenset[Freshness] = frozenset({"invalidated", "dirty"})
"""The states that refuse effects (D9) and hold readers."""


@dataclass(frozen=True)
class VersionedSourceSpec:
    """A versioned source: its name, the folder it reads (``root``) and its collections.

    The loader refuses a ``root`` inside an agent folder (ADR-P20 S3), because every file
    there enters the agent definition version hash. ``max_age_s`` is ADR-P25's.
    """

    name: str
    root: str | None = None
    collections: tuple[str, ...] = ()
    max_age_s: int | None = None


@dataclass(frozen=True)
class ReadStatus:
    """One read of a version and its state; ``now`` is the version or hash it moved to."""

    input: InputRef
    state: ReadState
    now: str | None = None

    def as_json(self) -> dict[str, Any]:
        """The status as a diff review's card records it."""
        return {"input": self.input.as_json(), "state": self.state, "now": self.now}


@dataclass(frozen=True)
class VersionState:
    """One version in the invalidation view.

    ``human_reviewed`` is True for a version a person chose, accepted or approved.
    ``via`` is one shortest lineage chain back to the change: this ref first, then each
    version it waits on, ending at the moved input. ``line`` names the runs the version
    was written for (its run, or a rebuild run's ``for_runs``; S1).
    """

    ref: ArtifactRef
    freshness: Freshness
    reads: tuple[ReadStatus, ...]
    is_latest: bool
    human_reviewed: bool
    writer: WriterId
    run: str | None
    event_seq: int
    open_revision: ArtifactRef | None = None
    via: tuple[str, ...] = ()
    line: tuple[str, ...] = ()
    origin: str = "task"

    @property
    def stale(self) -> bool:
        """Whether the version is invalidated or dirty (D9 refuses effects on it)."""
        return self.freshness in STALE_STATES

    def changed_reads(self) -> tuple[ReadStatus, ...]:
        """The reads that moved or are gone: why the version is invalidated."""
        return tuple(r for r in self.reads if r.state in ("moved", "gone"))


@dataclass(frozen=True)
class ChangeSummary:
    """The five lists of D12, each of refs as text, in save order.

    ``invalidated``: current versions whose inputs moved. ``rebuilt``: versions saved
    after ``since_seq`` that replaced an older version of their slot. ``current_by_cutoff``:
    versions kept current because a rebuilt input was equal. ``waiting``: open diff
    reviews' old versions. ``blocked``: invalidated versions held behind another read or a
    rejected review.
    """

    invalidated: tuple[str, ...] = ()
    rebuilt: tuple[str, ...] = ()
    current_by_cutoff: tuple[str, ...] = ()
    waiting: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()


@dataclass(frozen=True)
class RebuildEntry:
    """One invalidated version the plan rebuilds, by whom, how, and why.

    ``held_by`` names the reads that hold the writer (an input invalidated, dirty or under
    an open diff review); a held entry is listed so it can be shown, and is not entered.
    """

    writer: WriterId
    mode: RebuildMode
    invalidated: ArtifactRef
    because: tuple[ReadStatus, ...]
    run: str | None = None
    held_by: tuple[str, ...] = ()

    @property
    def runnable(self) -> bool:
        """Whether the writer may be re-entered now."""
        return not self.held_by


@dataclass(frozen=True)
class RebuildBrief:
    """What a rebuilt instance's fresh context starts from (D10, decision 34).

    The old version and its content, the reads that changed with the version each moved
    to, the reads to take instead (``current_reads``), and the statements the person
    already answered for this writer, so it does not ask them again.
    """

    old: ArtifactRef
    content: Mapping[str, Any]
    changed: tuple[ReadStatus, ...]
    current_reads: tuple[InputRef, ...]
    statements: tuple[InputRef, ...]
    via: tuple[str, ...]


@dataclass(frozen=True)
class Hunk:
    """One field-level change: a JSON pointer, its old and its new value.

    Keyed lists are matched by their key field, so the pointer names the row's key
    (``/rows/C1/state``). An absent value is ``None``.
    """

    path: str
    old: Any
    new: Any

    def as_json(self) -> dict[str, Any]:
        """The hunk as a diff review's card records it."""
        return {"path": self.path, "old": self.old, "new": self.new}


@dataclass(frozen=True)
class DiffReview:
    """A diff review: the payload of ``interrupt.raised{kind: review}`` (D1).

    ``card_hash`` covers everything the person sees; an acceptance naming another hash is
    refused.
    """

    interrupt_id: str
    old: ArtifactRef
    revision: ArtifactRef
    changed_reads: tuple[ReadStatus, ...]
    via: tuple[str, ...]
    hunks: tuple[Hunk, ...]
    card_hash: str
    run: str | None = None
    instance: str | None = None


@dataclass(frozen=True)
class SourceFacts:
    """Per-owner source facts read from the log: what ``source.changed`` last recorded.

    ``sources`` maps a source to its last recorded version, ``records`` a record ID to its
    last recorded version (None: the record is gone), and ``queries`` a recorded query to
    its latest result-set hash. ``source_version`` counts the owner's source changes and
    expiries. ``expired_records`` maps a record to the seq of its last ``source.expired``
    (ADR-P25 D3): a version saved before it read the record too long ago.
    ``expired_artifacts`` are the versions whose own lifetime ran out.
    """

    source_version: int = 0
    sources: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    records: Mapping[str, str | None] = field(default_factory=lambda: MappingProxyType({}))
    queries: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    last_change_seq: int = 0
    expired_records: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    expired_artifacts: frozenset[str] = frozenset()
