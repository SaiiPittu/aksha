"""A bench for the invalidation tests: one SQLite store, fake sources, and the demo chain.

``Bench`` records runs, reads, saves and approvals the way the runtime does: a save's
facts, the revocations and review voids it confirms, and a diff review when asked, all in
one step result (U2). ``FakeSource`` is a per-owner versioned source whose change sets the
tests apply. ``build_demo`` writes the change-and-recovery demo's starting state (ADR-P05
worked example) under neutral names: candidate C1's chain must reopen and C2's must not.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.artifacts.lineage import reads_to_record
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import (
    ArtifactKind,
    ArtifactRef,
    ArtifactVersion,
    InputRef,
    Origin,
    Saved,
    SaveRequest,
    WriterId,
)
from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue, EventTypeSpec
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution.ids import LogIdSource
from aksha.runtime.invalidation.confirm import confirmations
from aksha.runtime.invalidation.revision import diff_review, review_raised
from aksha.runtime.invalidation.types import DiffReview
from aksha.runtime.invalidation.view import InvalidationView, invalidation_view
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.storage.ports import EventStore
from tests.support.events import START, TEST_TYPES

OWNER = "o1"
OBJECT: Mapping[str, Any] = {"type": "object"}

APPROVAL_GRANTED = EventTypeSpec(
    name="approval.granted",
    schema={
        "type": "object",
        "properties": {"approval": {"type": "string"}, "targets": {"type": "array"}},
        "required": ["approval", "targets"],
    },
    writer="tests",
    boundaries=frozenset({"U4"}),
    dedup_key="decide:{approval}",
)
"""Stands in for ADR-P17's decision: a person approved an action on ``targets``."""


def catalogue() -> Catalogue:
    """The core catalogue, the probe types and the approval stand-in."""
    return Catalogue((*CORE_EVENT_TYPES, *TEST_TYPES, APPROVAL_GRANTED))


def kind(name: str, **kw: Any) -> ArtifactKind:
    return ArtifactKind(name, OBJECT, **kw)


KINDS: Mapping[str, ArtifactKind] = {
    "p.extract": kind("extract", keyed=True),
    "p.prior": kind("prior", keyed=True),
    "p.compare": kind("compare", keyed=True, list_keys={"/rows": "id"}),
    "p.recommend": kind("recommend", keyed=True),
    "p.table": kind("table"),
    "p.selection": kind("selection", reviewable=True),
    "p.form": kind("form", keyed=True),
    "p.packet": kind("packet", keyed=True, reviewable=True),
    "p.note": kind("note", keyed=True),
    "r.finding": kind("finding", keyed=True),
    "r.matrix": kind("matrix"),
}
"""Two agents: ``p``, whose chain the demo reopens, and ``r``, whose work it must not touch."""


def record(n: str, version: str = "1") -> InputRef:
    """Source record ``refs/<n>``."""
    return InputRef("record", f"refs/{n}", version)


def query(n: str, digest: str = "h1") -> InputRef:
    """Recorded query ``<n>`` with result-set hash ``digest``."""
    return InputRef("query", n, digest)


def ref(text: str) -> ArtifactRef:
    return ArtifactRef.parse(text)


@dataclass
class FakeSource:
    """A versioned source of ``refs/*`` records, per owner; ``change`` applies a change set."""

    name: str = "refs"
    collections: tuple[str, ...] = ("refs",)
    records: dict[str, dict[str, str | None]] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)

    def version(self, owner: str) -> str:
        return f"sv{self.versions.get(owner, 0)}"

    def record_version(self, owner: str, record_id: str) -> str | None:
        return self.records.get(owner, {}).get(record_id, "1")

    def change(self, owner: str, moved: Mapping[str, str | None]) -> None:
        self.records.setdefault(owner, {}).update(moved)
        self.versions[owner] = self.versions.get(owner, 0) + 1


@dataclass
class FakeQueries:
    """Result-set hashes per owner and query; unknown queries hash as ``h1``."""

    hashes: dict[tuple[str, str], str] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, owner: str, q: str, /) -> str:
        self.calls.append((owner, q))
        return self.hashes.get((owner, q), "h1")


class Bench:
    """One SQLite event store and an artifact store over the demo kinds."""

    def __init__(self, tmp: Path, owner: str = OWNER) -> None:
        self.clock = FakeClock(START)
        self.store: EventStore = open_store(
            tmp / "aksha.sqlite", self.clock, writer=True, catalogue=catalogue()
        )
        self.owner = owner
        self.artifacts = ArtifactStore(KINDS)
        self.ids = LogIdSource(self.store)
        self.steps = 0

    def close(self) -> None:
        self.store.close()

    def view(self, owner: str | None = None) -> LogView:
        return load_view(self.store, owner or self.owner)

    def iv(self, owner: str | None = None) -> InvalidationView:
        return invalidation_view(self.view(owner))

    def events(self, owner: str | None = None) -> list[Event]:
        return list(self.store.read(owner or self.owner))

    def types(self, owner: str | None = None) -> list[str]:
        return [e.type for e in self.events(owner)]

    def start(self, run: str, agent: str = "p", **payload: Any) -> None:
        body = {"agent": agent, "cause": "request", **payload}
        boundary = "U9" if body["cause"] == "rebuild" else "U1"
        self.store.append(self.owner, boundary, (NewEvent("run.started", body, run=run),))

    def rebuild_run(self, run: str, for_runs: Iterable[str], agent: str = "p") -> None:
        """Open ``run`` as a rebuild run for ``for_runs`` (by hand, for the unit tests)."""
        self.start(run, agent=agent, cause="rebuild", for_runs=list(for_runs),
                   rebuild_key=f"rebuild:{self.owner}/{agent}/{run}")  # fmt: skip

    def end(self, run: str, outcome: str = "complete") -> None:
        body = {"outcome": outcome, "reason": "done"}
        self.store.append(self.owner, "U3", (NewEvent("run.ended", body, run=run),))

    def read(self, run: str, instance: str, *inputs: InputRef) -> None:
        events = reads_to_record(self.view(), inputs, run=run, instance=instance)
        if events:
            self.store.append(self.owner, "U2", events)

    def save(
        self,
        run: str,
        slot: str,
        content: Mapping[str, Any],
        *,
        reads: Iterable[InputRef] = (),
        writer: WriterId | None = None,
        origin: Origin = "task",
        review: str | None = None,
        instance: str | None = None,
    ) -> Saved:
        """Save ``slot`` (``kind/key`` or ``kind``) after reading ``reads``, as one step.

        The writer defaults to the agent of the kind with the slot as its Task path. With
        ``review``, the save is a pending revision and its diff review (that interrupt ID)
        commits with it. Every save commits what it confirms (revocations, review voids).
        ``instance`` defaults to the writer's Task path; an instance's save records every
        read it made in the run, so a second save of one slot in one run takes a new one.
        """
        kind_name, _, key = slot.partition("/")
        agent = kind_name.split(".", 1)[0]
        writer = writer or WriterId(agent, slot.replace("/", "_"), None)
        instance = instance or writer.task_path
        self.steps += 1
        step = f"{run}/{instance}/t01/c{self.steps:02d}"
        view = self.view()
        pending = tuple(reads)
        recorded = reads_to_record(view, pending, run=run, instance=instance, step=step)
        saved = self.artifacts.save(
            view, SaveRequest(kind_name, key or None, content), writer=writer, run=run,
            instance=instance, step=step, writes=frozenset(KINDS), pending_reads=pending,
            origin=origin,
        )  # fmt: skip
        events: list[NewEvent] = [*recorded, *saved.events]
        if review is not None:
            events.append(review_raised(self.review_card(saved.version, review, run, instance)))
        if saved.events:
            events.extend(confirmations(view, events, saved.rows))
        if events:
            self.store.append(self.owner, "U2", events, saved.rows)
        return saved

    def review_card(
        self, revision: ArtifactVersion, interrupt: str, run: str, instance: str
    ) -> DiffReview:
        view = self.view()
        iv = invalidation_view(view)
        old = self.artifacts.current(view, revision.ref.kind, revision.ref.key)
        assert old is not None
        return diff_review(
            old, iv.state(old.ref), revision, KINDS[revision.ref.kind], interrupt,
            run=run, instance=instance,
        )  # fmt: skip

    def approve(self, approval: str, *targets: str) -> None:
        body = {"approval": approval, "targets": list(targets)}
        self.store.append(self.owner, "U4", (NewEvent("approval.granted", body),))

    def latest(self, slot: str) -> ArtifactVersion | None:
        kind_name, _, key = slot.partition("/")
        return self.artifacts.latest(self.view(), kind_name, key or None)

    def current(self, slot: str) -> ArtifactVersion | None:
        kind_name, _, key = slot.partition("/")
        return self.artifacts.current(self.view(), kind_name, key or None)


SELECT = WriterId("p", "select", None)
"""The selection's writer: one Task whose content a person chose."""


def build_demo(b: Bench, run: str = "R-0001") -> None:
    """The demo's starting state: two candidates' chains, a selection, packets, one approval.

    C1 and C2 each have an extraction (reads a contributor record), a prior-art result
    (reads a recorded query), a comparison, a recommendation, a form (reads the selection
    and the extraction) and a packet (reads the form). The table reads both
    recommendations, the selection (a person's choice) reads the table, and the packet of
    C1 carries approval A-0001. Agent ``r`` has research work nobody in ``p`` read.
    """
    b.start("R-0009", agent="r")
    b.save("R-0009", "r.finding/F1", {"text": "unrelated"}, reads=[record("W-1")])
    b.save("R-0009", "r.matrix", {"cells": 1}, reads=[ref("r.finding/F1@v1").as_input()])
    b.end("R-0009")
    b.start(run)
    for c in ("C1", "C2"):
        b.save(run, f"p.extract/{c}", {"contributors": ["a"]}, reads=[record(f"K-{c}")])
        b.save(run, f"p.prior/{c}", {"families": ["A"]}, reads=[query(f"Q-{c}")])
        b.save(run, f"p.compare/{c}", {"rows": [{"id": "F1", "state": "partial"}]},
               reads=[ref(f"p.prior/{c}@v1").as_input()])  # fmt: skip
        b.save(run, f"p.recommend/{c}", {"go": True},
               reads=[ref(f"p.compare/{c}@v1").as_input()])  # fmt: skip
    both = [ref("p.recommend/C1@v1").as_input(), ref("p.recommend/C2@v1").as_input()]
    b.save(run, "p.table", {"order": ["C1", "C2"]}, reads=both)
    b.save(run, "p.selection", {"chosen": ["C1", "C2"]},
           reads=[ref("p.table@v1").as_input()], writer=SELECT, origin="choice")  # fmt: skip
    for c in ("C1", "C2"):
        inputs = [ref("p.selection@v1").as_input(), ref(f"p.extract/{c}@v1").as_input()]
        b.save(run, f"p.form/{c}", {"names": ["a"]}, reads=inputs)
        b.save(run, f"p.packet/{c}", {"forms": [c]}, reads=[ref(f"p.form/{c}@v1").as_input()])
    b.approve("A-0001", "p.packet/C1@v1")
    b.end(run)
