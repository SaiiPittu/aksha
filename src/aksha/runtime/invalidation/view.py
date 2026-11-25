"""The invalidation view (ADR-P05 D3, D4, S1, S3): a projection over the log, never stored.

The stored facts are ``source.changed``, ``query.reexecuted``, the artifact saves, the
diff reviews and the approvals; every state here is recomputed from them, so it cannot
disagree with them after a crash. Each read of a version gets one state:

1. A record read is ``moved`` when the owner's last ``source.changed`` gave the record
   another version, and ``gone`` when it gave None. A query read is ``moved`` when its
   last ``query.reexecuted`` hash differs. Statements and blobs are ``same`` (a statement
   is superseded only once ADR-P17 records that; a blob is content-addressed).
2. An artifact read is ``gone`` when the version is not stored or its writer instance is
   withdrawn (S3). Otherwise the slot's newest version that counts for this reader is
   compared with the version read: none newer, ``same`` (``unsettled`` if the version
   read is itself invalidated or dirty); a newer one with an equal canonical hash (or, for
   a path-scoped read, an equal entry), ``equivalent``, or ``unsettled`` while that newer
   one is stale; a newer one with another hash, ``moved``.
3. The sibling rule (S1). A newer version counts only when one of the runs it was written
   for is a run the reader or the version read was written for: a request run's version
   is written for that run, a rebuild run's for its ``for_runs``. A version of an
   unrelated request, or of a rebuild for another run, is a sibling and moves nobody.
4. A pending revision (an open diff review) and a rejected or voided one never count:
   readers see the old version until a person accepts (D1).

A version is ``invalidated`` if any read moved or is gone, else ``dirty`` if any is
unsettled, else ``current_by_cutoff`` if any is equivalent, else ``current``.

Amendment from the build: the ADR's "one pass in save order" holds for every read of an
earlier version, but rule 2 also looks at the newer version of a slot, which is saved
later. The view therefore evaluates versions in save order with memoised recursion,
which visits each version once and equals a naive fixpoint
(``test_invalidation_one_pass_equals_fixpoint``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from aksha.runtime.artifacts.hashing import entry_hash
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.types import ArtifactRef, ArtifactVersion, InputRef, WriterId
from aksha.runtime.artifacts.versions import all_versions, unaccepted_revisions
from aksha.runtime.invalidation.reviews import approved_refs, open_reviews, reviews
from aksha.runtime.invalidation.sources import source_facts
from aksha.runtime.invalidation.types import (
    STALE_STATES,
    ChangeSummary,
    Freshness,
    ReadStatus,
    SourceFacts,
    VersionState,
)
from aksha.runtime.projections.view import LogView

HUMAN_ORIGINS = frozenset({"choice", "copy_choice"})
"""Origins whose content a person chose: such a version is human-reviewed (D1)."""


@dataclass(frozen=True)
class InvalidationView:
    """Every version's state for one owner as of ``as_of_seq``, keyed by ref.

    ``unaccepted`` holds pending revisions and rejected or voided ones, which are in
    ``versions`` with ``is_latest`` False. ``rejected`` holds old versions whose last
    review was rejected (S4): their writer is blocked until a new version is saved.
    """

    owner: str
    as_of_seq: int
    versions: Mapping[str, VersionState]
    unaccepted: frozenset[str] = frozenset()
    rejected: frozenset[str] = frozenset()
    withdrawn: frozenset[WriterId] = frozenset()
    sources: SourceFacts = field(default_factory=SourceFacts)

    def state(self, ref: ArtifactRef | str) -> VersionState:
        """The state of ``ref`` (``KeyError`` if the view holds no such version)."""
        return self.versions[str(ref)]

    def latest(self) -> tuple[VersionState, ...]:
        """Each line's latest version (see the module doc's rule 3), in save order."""
        return tuple(v for v in self.versions.values() if v.is_latest)

    def invalidated_latest(self) -> tuple[VersionState, ...]:
        """The latest versions that are invalidated, oldest first."""
        return tuple(v for v in self.latest() if v.freshness == "invalidated")

    def refs(self, freshness: Freshness) -> tuple[str, ...]:
        """The latest versions in ``freshness``, as text, oldest first."""
        return tuple(str(v.ref) for v in self.latest() if v.freshness == freshness)

    def summary(self, since_seq: int = 0) -> ChangeSummary:
        """The five lists of D12, for what happened after ``since_seq``."""
        latest = self.latest()
        rebuilt = tuple(str(v.ref) for v in latest if v.event_seq > since_seq and v.ref.version > 1)
        waiting = tuple(str(v.ref) for v in latest if v.open_revision is not None)
        blocked = tuple(
            str(v.ref)
            for v in latest
            if v.freshness == "invalidated"
            and v.open_revision is None
            and (str(v.ref) in self.rejected or any(r.state == "unsettled" for r in v.reads))
        )
        return ChangeSummary(
            invalidated=self.refs("invalidated"),
            rebuilt=rebuilt,
            current_by_cutoff=self.refs("current_by_cutoff"),
            waiting=waiting,
            blocked=blocked,
        )


def runs_for(view: LogView) -> Mapping[str, frozenset[str]]:
    """Each run to the runs it writes for: itself, or a rebuild run's ``for_runs`` (S1)."""
    out: dict[str, frozenset[str]] = {}
    for e in view.events:
        if e.type == "run.started" and e.run is not None:
            p = e.payload
            if p.get("cause") == "rebuild" and p.get("for_runs"):
                out[e.run] = frozenset(str(r) for r in p["for_runs"])
            else:
                out[e.run] = frozenset({e.run})
    return MappingProxyType(out)


def invalidation_view(
    view: LogView,
    *,
    withdrawn: Iterable[WriterId] = (),
    blobs: BlobStore | None = None,
) -> InvalidationView:
    """The invalidation view of ``view`` (module doc). Pure.

    ``withdrawn`` are writer instances the scheduler withdrew (S3; ADR-P15 records them
    once it is built): their versions read as ``gone``.
    """
    return _Walk(view, frozenset(withdrawn), blobs).run()


class _Walk:
    """One evaluation of the view: memoised states over the versions in save order."""

    def __init__(self, view: LogView, withdrawn: frozenset[WriterId], blobs: BlobStore | None):
        self.view = view
        self.withdrawn = withdrawn
        self.all = all_versions(view, blobs=blobs)
        self.by_ref = {str(v.ref): v for v in self.all}
        self.unaccepted = unaccepted_revisions(view)
        self.facts = source_facts(view)
        self.lines = runs_for(view)
        slots: dict[tuple[str, str | None], list[ArtifactVersion]] = {}
        for v in sorted(self.all, key=lambda v: v.ref.version):
            if str(v.ref) not in self.unaccepted:
                slots.setdefault(v.ref.slot(), []).append(v)
        self.slots = slots
        self.reviewed = approved_refs(view) | {
            str(r.revision) for r in reviews(view) if r.outcome == "accepted"
        }
        self.open = open_reviews(view)
        self.done: dict[str, tuple[Freshness, tuple[ReadStatus, ...], tuple[str, ...]]] = {}
        self.busy: set[str] = set()

    def run(self) -> InvalidationView:
        states: dict[str, VersionState] = {}
        for v in sorted(self.all, key=lambda v: v.event_seq):
            freshness, reads, via = self.evaluate(v)
            ref = str(v.ref)
            review = self.open.get(ref)
            states[ref] = VersionState(
                ref=v.ref, freshness=freshness, reads=reads, is_latest=self.is_latest(v),
                human_reviewed=self.human_reviewed(v), writer=v.writer, run=v.run,
                event_seq=v.event_seq, open_revision=None if review is None else review.revision,
                via=via, line=tuple(sorted(self.line(v))), origin=v.origin,
            )  # fmt: skip
        return InvalidationView(
            owner=self.view.owner,
            as_of_seq=self.view.seq_read,
            versions=MappingProxyType(states),
            unaccepted=self.unaccepted,
            rejected=self.rejected(),
            withdrawn=self.withdrawn,
            sources=self.facts,
        )

    # --- per-version facts -----------------------------------------------------------

    def line(self, v: ArtifactVersion) -> frozenset[str]:
        if v.run is None:
            return frozenset()
        return self.lines.get(v.run, frozenset({v.run}))

    def human_reviewed(self, v: ArtifactVersion) -> bool:
        return v.origin in HUMAN_ORIGINS or str(v.ref) in self.reviewed

    def is_latest(self, v: ArtifactVersion) -> bool:
        if str(v.ref) in self.unaccepted:
            return False
        mine = self.line(v)
        return not any(
            n.ref.version > v.ref.version and self.line(n) & mine
            for n in self.slots.get(v.ref.slot(), ())
        )

    def rejected(self) -> frozenset[str]:
        """Old versions whose last review was rejected and whose slot saw no save since."""
        out: set[str] = set()
        for r in reviews(self.view):
            if r.outcome != "rejected" or r.closed_seq is None:
                continue
            since = [
                v
                for v in self.slots.get(r.old.slot(), ())
                if v.event_seq > r.closed_seq and v.ref.version > r.old.version
            ]
            if not since:
                out.add(str(r.old))
        return frozenset(out)

    # --- the states ------------------------------------------------------------------

    def evaluate(
        self, v: ArtifactVersion
    ) -> tuple[Freshness, tuple[ReadStatus, ...], tuple[str, ...]]:
        ref = str(v.ref)
        if ref in self.done:
            return self.done[ref]
        if ref in self.busy:  # a newer version that read this one: assume it settled
            return ("current", (), ())
        self.busy.add(ref)
        statuses: list[ReadStatus] = []
        waits: list[tuple[str, ...]] = []
        for read in v.reads:
            status, waits_on = self.read_state(v, read)
            statuses.append(status)
            if waits_on is not None:
                waits.append(self.evaluate(self.by_ref[waits_on])[2])
        self.busy.discard(ref)
        states = {s.state for s in statuses}
        via: tuple[str, ...] = ()
        if states & {"moved", "gone"}:
            freshness: Freshness = "invalidated"
            cause = next(s for s in statuses if s.state in ("moved", "gone"))
            via = (ref, str(cause.input))
        elif "unsettled" in states:
            freshness = "dirty"
            via = (ref, *min(waits, key=len, default=()))
        elif "equivalent" in states:
            freshness = "current_by_cutoff"
        else:
            freshness = "current"
        result = (freshness, tuple(statuses), via)
        self.done[ref] = result
        return result

    def stale(self, v: ArtifactVersion) -> bool:
        return self.evaluate(v)[0] in STALE_STATES

    def read_state(self, reader: ArtifactVersion, read: InputRef) -> tuple[ReadStatus, str | None]:
        """One read's status, and the ref it waits on when unsettled."""
        if read.kind == "record":
            return self._versioned(read, self.facts.records), None
        if read.kind == "query":
            return self._versioned(read, self.facts.queries), None
        if read.kind != "artifact":
            return ReadStatus(read, "same"), None
        whole = ArtifactRef.from_input(read)
        target = self.by_ref.get(str(whole))
        if target is None or target.writer in self.withdrawn:
            return ReadStatus(read, "gone"), None
        newest = self.newest_counting(reader, target)
        if newest is None:
            if self.stale(target):
                return ReadStatus(read, "unsettled"), str(target.ref)
            return ReadStatus(read, "same"), None
        now = str(newest.ref)
        if not _equal(read, target, newest):
            return ReadStatus(read, "moved", now), None
        if self.stale(newest):
            return ReadStatus(read, "unsettled", now), now
        return ReadStatus(read, "equivalent", now), None

    def newest_counting(
        self, reader: ArtifactVersion, target: ArtifactVersion
    ) -> ArtifactVersion | None:
        """The slot's newest version after ``target`` that counts for ``reader`` (rule 3)."""
        mine = self.line(reader) | self.line(target)
        later = [
            n
            for n in self.slots.get(target.ref.slot(), ())
            if n.ref.version > target.ref.version and self.line(n) & mine
        ]
        return later[-1] if later else None

    @staticmethod
    def _versioned(read: InputRef, known: Mapping[str, str | None]) -> ReadStatus:
        if read.id not in known:
            return ReadStatus(read, "same")
        now = known[read.id]
        if now is None:
            return ReadStatus(read, "gone")
        return ReadStatus(read, "same" if now == read.version else "moved", now)


def _equal(read: InputRef, old: ArtifactVersion, new: ArtifactVersion) -> bool:
    """Early cutoff: an equal canonical hash, or for a path-scoped read an equal entry."""
    if read.path is not None and read.entry_hash is not None:
        try:
            return entry_hash(new.content, read.path) == read.entry_hash
        except KeyError:
            return False
    return old.canonical_hash == new.canonical_hash
