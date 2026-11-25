"""``LogView`` and ``ArtifactsView``: read-only snapshots of one owner's event store.

Every reducer, completion check and stop policy reads only a view; a view holds no store
handle, so nothing that reads it can write. ``load_view`` is the one place a view is read
from a store.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from aksha.runtime.events.envelope import Event
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.storage.rows import StoredArtifact


@dataclass(frozen=True)
class ArtifactsView:
    """One owner's artifact rows whose saved event is at or before ``seq_read``.

    ``upto`` makes a narrower view that shares the rows, so a reducer can be shown exactly
    the rows committed up to the event it is applying, at no copying cost.
    """

    rows: tuple[StoredArtifact, ...]
    seq_read: int = -1
    _by_ref: Mapping[str, StoredArtifact] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Default ``seq_read`` to the newest row, and index the rows by ref."""
        if self.seq_read < 0:
            newest = max((a.event_seq for a in self.rows), default=0)
            object.__setattr__(self, "seq_read", newest)
        object.__setattr__(self, "_by_ref", MappingProxyType({a.ref: a for a in self.rows}))

    def get(self, ref: str) -> StoredArtifact | None:
        """The row for ``ref``, if it was committed by ``seq_read``."""
        found = self._by_ref.get(ref)
        return found if found is not None and found.event_seq <= self.seq_read else None

    def all(self) -> tuple[StoredArtifact, ...]:
        """Every visible row, in commit order."""
        return tuple(a for a in self.rows if a.event_seq <= self.seq_read)

    def upto(self, seq: int) -> ArtifactsView:
        """The same rows seen at ``seq``; a view never grows (``ValueError``)."""
        if seq > self.seq_read:
            raise ValueError(f"cannot widen a view read at {self.seq_read} to {seq}")
        narrowed = object.__new__(ArtifactsView)
        object.__setattr__(narrowed, "rows", self.rows)
        object.__setattr__(narrowed, "seq_read", seq)
        object.__setattr__(narrowed, "_by_ref", self._by_ref)
        return narrowed

    def __len__(self) -> int:
        """How many rows are visible."""
        return len(self.all())


@dataclass(frozen=True)
class LogView:
    """A read-only snapshot of one owner's log up to ``seq_read``.

    ``complete`` is True only for a view :func:`load_view` read: every event and artifact
    row of the owner up to ``seq_read``. A view built by hand, or from a filtered read (by
    run or by types), is not complete, and the projection cache never keeps its state.
    """

    owner: str
    seq_read: int
    events: tuple[Event, ...]
    artifacts: ArtifactsView
    complete: bool = False

    def upto(self, seq: int) -> LogView:
        """The same log as it stood at ``seq``; a view never grows (``ValueError``).

        Narrowing a complete view gives a complete view: it still holds every event up to
        its new ``seq_read``.
        """
        if seq > self.seq_read:
            raise ValueError(f"cannot widen a view read at {self.seq_read} to {seq}")
        events = tuple(e for e in self.events if e.seq <= seq)
        return LogView(self.owner, seq, events, self.artifacts.upto(seq), self.complete)


def load_view(store: EventStore, owner: str, upto_seq: int | None = None) -> LogView:
    """Read one owner's events and artifact rows up to ``upto_seq`` (default: the latest).

    The last seq is read first, so later commits never leak into the snapshot.
    """
    seq = store.last_seq(owner) if upto_seq is None else upto_seq
    events = tuple(store.read(owner, upto_seq=seq))
    artifacts = ArtifactsView(store.read_artifacts(owner, upto_seq=seq), seq)
    return LogView(owner, seq, events, artifacts, complete=True)
