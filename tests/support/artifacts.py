"""A small world for the artifact store tests: kinds, relations and a store on SQLite.

``World`` opens a SQLite event store in a temporary folder, records reads for a Task
instance the way the runtime will (``read.recorded`` in a step result), and commits what
a save returns, so each test reads like the scenario it checks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from aksha.adapters.blobs.filesystem import FilesystemBlobStore
from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.artifacts.lineage import reads_to_record
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import (
    ArtifactKind,
    InputRef,
    RelationKind,
    Saved,
    SaveRequest,
    WriterId,
)
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.storage.ports import EventStore
from tests.support.events import START, probe_catalogue

OWNER = "o1"
RUN = "R-0001"
OBJECT: Mapping[str, Any] = {"type": "object"}
LINE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "rows": {"type": "array"}},
    "required": ["text"],
}

KINDS: Mapping[str, ArtifactKind] = {
    "notes.line": ArtifactKind("line", LINE_SCHEMA, keyed=True),
    "notes.summary": ArtifactKind("summary", OBJECT),
    "notes.merge": ArtifactKind("merge", OBJECT, keyed=True),
    "notes.tree": ArtifactKind("tree", OBJECT, writers="chain"),
    "other.fact": ArtifactKind("fact", OBJECT, keyed=True),
}
"""Kinds by full name: an agent ``notes`` and a second agent ``other``."""

RELATIONS: Mapping[str, RelationKind] = {
    "notes.same_as": RelationKind("same_as", src=("record:pages",), dst=("record:pages",)),
    "notes.contradicts": RelationKind(
        "contradicts", src=("line",), dst=("line",), labels=("price", "date")
    ),
    "notes.summarises": RelationKind(
        "summarises", src=("summary",), dst=("line",), cites="per_end"
    ),
}

WRITES = frozenset(KINDS)
"""Every kind: the tests give rights explicitly where they check them."""

GATHER = WriterId("notes", "gather", None)
COMPARE = WriterId("notes", "compare", None)
OTHER = WriterId("other", "find", None)


def page(n: int, version: str = "1") -> InputRef:
    """Source record ``pages/P-000n`` at ``version``."""
    return InputRef("record", f"pages/P-{n:04d}", version)


class World:
    """One owner's event store, a blob store and an artifact store over both."""

    def __init__(self, tmp: Path) -> None:
        self.clock = FakeClock(START)
        self.store: EventStore = open_store(
            tmp / "aksha.sqlite", self.clock, writer=True, catalogue=probe_catalogue()
        )
        self.blobs = FilesystemBlobStore(tmp / "blobs")
        self.artifacts = ArtifactStore(KINDS, RELATIONS, blobs=self.blobs)
        self._steps = 0

    def view(self) -> LogView:
        """The owner's log now."""
        return load_view(self.store, OWNER)

    def read(self, instance: str, *inputs: InputRef, run: str = RUN) -> None:
        """Record ``inputs`` as read by ``instance``, as a tool result would."""
        events = reads_to_record(self.view(), inputs, run=run, instance=instance)
        if events:
            self.store.append(OWNER, "U2", events)

    def save(
        self,
        kind: str,
        key: str | None,
        content: Mapping[str, Any],
        *,
        writer: WriterId = GATHER,
        instance: str = "gather",
        run: str = RUN,
        writes: Iterable[str] = WRITES,
        **request: Any,
    ) -> Saved:
        """Save through the store and commit what it returns, as a step result would."""
        self._steps += 1
        saved = self.artifacts.save(
            self.view(),
            SaveRequest(kind, key, content, **request),
            writer=writer,
            run=run,
            instance=instance,
            step=f"{run}/{instance}/t01/c{self._steps:02d}",
            writes=frozenset(writes),
        )
        if saved.events:
            self.store.append(OWNER, "U2", saved.events, saved.rows)
        return saved

    def close(self) -> None:
        """Close the event store."""
        self.store.close()
