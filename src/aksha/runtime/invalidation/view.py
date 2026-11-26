"""The invalidation view (ADR-P05 D3, D4, S1, S3): a projection over the log, never stored.

The stored facts are ``source.changed``, ``query.reexecuted``, the artifact saves, the
diff reviews and the approvals; every state here is recomputed from them, so it cannot
disagree with them after a crash. Each read of a version gets one state:

1. A record read is ``moved`` when the owner's last ``source.changed`` gave the record
   another version, and ``gone`` when it gave None. A query read is ``moved`` when its
   last ``query.reexecuted`` hash differs. A statement read (an answer, named by its
   interrupt and answer) is ``moved`` once a later statement span ``supersedes`` its span
   (an amendment, ADR-P27 D5), else ``same``; a blob is content-addressed, so ``same``.
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
5. Time (ADR-P25). A ``time:clock`` read is checked like a record read against the
   clock record ``time/clock``, so a later recorded date moves it (D4). A record read of a
   version saved before the record's last ``source.expired`` is ``moved`` to ``expired``
   (D3): the fetch it was built on is past its freshness lifetime; a version saved after
   the expiry read it again and is not. A version whose own lifetime ran out (its kind's
   ``max_age_s``) carries one more read, ``time:expiry/<ref>``, ``moved`` to ``expired``,
   so the plan, its brief and the revocations treat it as any invalidated version.
6. Supersession (ADR-P09 D4, D11). A newer version that carries a ``supersedes`` relation
   counts for every reader, whatever its run: it replaces the slot for everyone, so the
   sibling rule yields, and every older version stops being the latest of any line. A
   version never moves itself or a later version of its own slot: a save that read its
   slot's earlier version is checked against versions older than itself only. Nor does a
   superseding version move a reader its own writer saved later in the same run: an
   intake instance that recalled v1, saved v2 over it and then saved its scope saw both
   (ADR-P09 D4, M5). A plain revision in the same run still moves that reader.

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
from aksha.runtime.grounding.spans import span_index
from aksha.runtime.grounding.types import STATEMENT_SOURCE
from aksha.runtime.invalidation.reviews import approved_refs, open_reviews, reviews
from aksha.runtime.invalidation.sources import record_key, source_facts
from aksha.runtime.invalidation.types import (
    EXPIRED,
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


def superseded_statements(view: LogView) -> Mapping[tuple[str, str], str]:
    """Each superseded answer, by (interrupt, answer), to the answer that replaced it last."""
    index = span_index(view)
    by_id = {s.span_id: s for s in index.spans.values() if s.source == STATEMENT_SOURCE}
    newer = {s.supersedes: s for s in by_id.values() if s.supersedes in by_id}
    out: dict[tuple[str, str], str] = {}
    for old in by_id.values():
        last = newer.get(old.span_id)
        if last is None:
            continue
        while last.span_id in newer:
            last = newer[last.span_id]
        out[(old.record_id, old.version)] = last.version
    return MappingProxyType(out)


def withdrawn_writers(view: LogView) -> frozenset[WriterId]:
    """The writer keys whose last lifecycle fact is a ``task.withdrawn`` (S3, ADR-P15 D3).

    A writer key names one instance in every run of its agent, so the last of its
    ``task.entered`` (which names the writer) and ``task.withdrawn`` (which names the Task
    path and key; the agent is the run's) decides, across runs: a key that came back and
    was entered again is no longer withdrawn.
    """
    agents: dict[str, str] = {}
    last: dict[WriterId, bool] = {}
    for e in view.events:
        if e.type == "run.started" and e.run is not None:
            agents[e.run] = str(e.payload.get("agent", ""))
        elif e.type == "task.entered":
            w = e.payload.get("writer")
            if isinstance(w, Mapping):
                last[WriterId(str(w["agent"]), str(w["task_path"]), _key(w.get("key")))] = False
        elif e.type == "task.withdrawn" and e.run in agents:
            p = e.payload
            last[WriterId(agents[e.run], str(p["task"]), _key(p.get("key")))] = True
    return frozenset(w for w, gone in last.items() if gone)


def _key(value: object) -> str | None:
    return None if value is None else str(value)


def invalidation_view(
    view: LogView,
    *,
    withdrawn: Iterable[WriterId] = (),
    blobs: BlobStore | None = None,
) -> InvalidationView:
    """The invalidation view of ``view`` (module doc). Pure.

    The withdrawn writers (S3) are read from the log's ``task.withdrawn`` facts
    (:func:`withdrawn_writers`); ``withdrawn`` adds more, for a caller that knows of a
    withdrawal not yet committed. Their versions read as ``gone``.
    """
    every = withdrawn_writers(view) | frozenset(withdrawn)
    return _Walk(view, every, blobs).run()


class _Walk:
    """One evaluation of the view: memoised states over the versions in save order."""

    def __init__(self, view: LogView, withdrawn: frozenset[WriterId], blobs: BlobStore | None):
        self.view = view
        self.withdrawn = withdrawn
        self.all = all_versions(view, blobs=blobs)
        self.by_ref = {str(v.ref): v for v in self.all}
        self.unaccepted = unaccepted_revisions(view)
        self.facts = source_facts(view)
        self.statements = superseded_statements(view)
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
        self.superseding = frozenset(
            str(e.payload.get("carrier"))
            for e in view.events
            if e.type == "relation.saved" and e.payload.get("kind") == "supersedes"
        )
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
            n.ref.version > v.ref.version and self.counts(n, mine)
            for n in self.slots.get(v.ref.slot(), ())
        )

    def counts(self, newer: ArtifactVersion, line: frozenset[str]) -> bool:
        """Whether ``newer`` counts for a reader of ``line``: one run in common, or rule 6."""
        return bool(self.line(newer) & line) or str(newer.ref) in self.superseding

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
        if ref in self.facts.expired_artifacts:
            statuses.append(ReadStatus(expiry_read(v.ref), "moved", EXPIRED))
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
        """One read's status, and the ref it waits on when unsettled.

        A superseded statement the reader also read the replacement of is ``same``: the
        version was saved with the amended answer in view (ADR-P27 D5).
        """
        status, waits = self.read_state_for(
            self.line(reader), read, reader.event_seq, reader=reader
        )
        seen = status.now is not None and InputRef("statement", read.id, status.now) in reader.reads
        if read.kind == "statement" and status.state == "moved" and seen:
            return ReadStatus(read, "same"), None
        return status, waits

    def read_state_for(
        self,
        mine: frozenset[str],
        read: InputRef,
        saved_seq: int | None = None,
        *,
        reader: ArtifactVersion | None = None,
    ) -> tuple[ReadStatus, str | None]:
        """One read's status for a reader written for the runs ``mine`` (rules 1 to 6).

        ``saved_seq`` is the reader's save; without it a record's expiry is not judged.
        ``reader`` is the version that made the read, when it is one: a later version of
        the read's slot never counts when it is the reader or newer, or when the reader's
        own writer saved it earlier in the reader's run (rule 6).
        """
        if read.kind == "time":
            return self._versioned(read, self.facts.records), None
        if read.kind == "record":
            status = self._versioned(read, self.facts.records)
            expired_at = self.facts.expired_records.get(read.id)
            expired = saved_seq is not None and expired_at is not None and expired_at > saved_seq
            if status.state == "same" and expired:
                return ReadStatus(read, "moved", EXPIRED), None
            return status, None
        if read.kind == "query":
            return self._versioned(read, self.facts.queries), None
        if read.kind == "statement":
            now = self.statements.get((read.id, read.version))
            return ReadStatus(read, "same" if now is None else "moved", now), None
        if read.kind != "artifact":
            return ReadStatus(read, "same"), None
        whole = ArtifactRef.from_input(read)
        target = self.by_ref.get(str(whole))
        if target is None or target.writer in self.withdrawn:
            return ReadStatus(read, "gone"), None
        newest = self.newest_counting(mine, target, reader=reader)
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
        self,
        reader_line: frozenset[str],
        target: ArtifactVersion,
        *,
        reader: ArtifactVersion | None = None,
    ) -> ArtifactVersion | None:
        """The slot's newest version after ``target`` that counts for the reader (rules 3, 6)."""
        mine = reader_line | self.line(target)
        later = [
            n
            for n in self.slots.get(target.ref.slot(), ())
            if n.ref.version > target.ref.version
            and self.counts(n, mine)
            and not (reader is not None and _seen_by(n, reader, self.superseding))
        ]
        return later[-1] if later else None

    @staticmethod
    def _versioned(read: InputRef, known: Mapping[str, str | None]) -> ReadStatus:
        key = record_key(read)
        if key not in known:
            return ReadStatus(read, "same")
        now = known[key]
        if now is None:
            return ReadStatus(read, "gone")
        return ReadStatus(read, "same" if now == read.version else "moved", now)


class ReadChecker:
    """The state of a read an instance made, by the view's own rules (2 to 4 and S1, S3).

    The context (ADR-P08, decision 32) asks it which of a tool result's reads went out of
    date, so masking and invalidation agree: a sibling's newer version (another run's
    line) moves nothing, an equal rebuild is ``equivalent`` (early cutoff) and not
    announced, and a withdrawn writer's version is ``gone``. Build one per view; it
    evaluates each version at most once.
    """

    def __init__(
        self, view: LogView, *, withdrawn: Iterable[WriterId] = (), blobs: BlobStore | None = None
    ) -> None:
        """Check reads against ``view`` (withdrawn writers read from it, plus ``withdrawn``)."""
        self._walk = _Walk(view, withdrawn_writers(view) | frozenset(withdrawn), blobs)

    def status(self, run: str, read: InputRef) -> ReadStatus:
        """The state of ``read`` for an instance of ``run`` (its line: S1)."""
        mine = self._walk.lines.get(run, frozenset({run}))
        return self._walk.read_state_for(mine, read)[0]

    def version(self, ref: str) -> ArtifactVersion | None:
        """The stored version ``ref`` names, accepted or not."""
        return self._walk.by_ref.get(ref)


def expiry_read(ref: ArtifactRef) -> InputRef:
    """The read an expired version carries (rule 5): its lifetime, as a time input."""
    return InputRef("time", f"expiry/{ref}", "current")


def is_expiry_read(read: InputRef) -> bool:
    """Whether ``read`` is the marker :func:`expiry_read` adds, not an input to read again."""
    return read.kind == "time" and read.id.startswith("expiry/")


def _equal(read: InputRef, old: ArtifactVersion, new: ArtifactVersion) -> bool:
    """Early cutoff: an equal canonical hash, or for a path-scoped read an equal entry."""
    if read.path is not None and read.entry_hash is not None:
        try:
            return entry_hash(new.content, read.path) == read.entry_hash
        except KeyError:
            return False
    return old.canonical_hash == new.canonical_hash


def _seen_by(newer: ArtifactVersion, reader: ArtifactVersion, superseding: frozenset[str]) -> bool:
    """Whether ``reader`` knew of ``newer`` when it was saved, so it cannot move it (rule 6).

    ``newer`` is the reader itself or a later version of the reader's own slot, or a
    superseding version the reader's own writer saved earlier in the reader's run.
    """
    if newer.ref.slot() == reader.ref.slot() and newer.ref.version >= reader.ref.version:
        return True
    return (
        str(newer.ref) in superseding
        and newer.writer == reader.writer
        and newer.run is not None
        and newer.run == reader.run
        and newer.event_seq < reader.event_seq
    )
