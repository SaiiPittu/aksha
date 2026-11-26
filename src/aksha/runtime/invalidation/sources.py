"""Source facts: the one writer of ``source.changed`` and ``query.reexecuted`` (ADR-P05 S2, D6).

``record_source_changes`` is the only function that builds either fact. The
``apply_source_change`` command and recovery's source check both call it, once per owner,
and commit what it returns in the source-change boundary (U8), with the revocations the
change confirms (D9). It asks each ``VersionedSource`` for the owner's versions, compares
them with the versions the owner's log last knew, and records only what moved:

1. A source whose version differs from the last recorded one (or that was never recorded)
   is checked record by record, for every record the owner has read from it. A source
   never recorded before is recorded when one of its read records moved, or when
   a change applied by a command (an upload) gives one of the owner's recorded queries a
   new result-set hash on re-execution: a file no work read yet can still change what a
   listing or a search returns (amendment from the ingestion build, 26 Nov 2026).
   Recovery's check does not re-execute queries for a source nothing read.
2. A record whose version differs from the one last known (recorded by a change, or else
   the newest read of it) is recorded with its new version, or None when it is gone.
3. Only when something is recorded are the owner's recorded queries re-executed, each
   writing ``query.reexecuted`` with its new result-set hash and whether it ``changed``.
   An unchanged source re-executes nothing.

ADR-P25 adds two inputs, through the same writer:

4. The clock source (``CLOCK_SOURCE``, D4) is the current date. Its one record,
   ``time/clock``, is checked against the owner's ``time:clock`` reads, and it is recorded
   even when nothing read it yet: its caller (``timers.clock.date_change``) asks only when
   an open run or an older read makes the new date matter.
5. Citation keying (amendment M2): for each moved record of a source that can hand back
   its text (``RecordTexts``), every span minted over the record is compared with the new
   text, and one ``excerpts.rechecked`` fact records each as ``kept``, ``changed`` or
   ``gone``, with the source change's number.
6. ``expired`` (D3) lists the records and artifact versions whose freshness lifetime ran
   out: one ``source.expired`` fact names them, counted as a source change.

``source_facts`` is the projection over those facts that the invalidation view reads.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType

from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.grounding.spans import span_index
from aksha.runtime.invalidation.keying import SpanCheck, recheck, rechecked_event
from aksha.runtime.invalidation.ports import QueryRunner, RecordTexts, VersionedSource
from aksha.runtime.invalidation.types import (
    CLOCK_RECORD,
    CLOCK_SOURCE,
    SourceCause,
    SourceFacts,
)
from aksha.runtime.projections.view import LogView

SOURCE_CHANGE_BOUNDARY = "U8"
SOURCE_EXPIRED = "source.expired"


def source_facts(view: LogView) -> SourceFacts:
    """The owner's source, record and query versions as its log last recorded them."""
    n, last = 0, 0
    sources: dict[str, str] = {}
    records: dict[str, str | None] = {}
    queries: dict[str, str] = {}
    expired: dict[str, int] = {}
    artifacts: set[str] = set()
    for e in view.events:
        p = e.payload
        if e.type == "source.changed":
            n, last = int(p["source_version"]), e.seq
            sources.update({str(k): str(v) for k, v in p["sources"].items()})
            records.update({str(k): None if v is None else str(v) for k, v in p["records"].items()})
        elif e.type == "query.reexecuted":
            queries[str(p["query"])] = str(p["hash"])
        elif e.type == SOURCE_EXPIRED:
            n, last = int(p["source_version"]), e.seq
            expired.update({str(r): e.seq for r in p.get("records", ())})
            artifacts.update(str(a) for a in p.get("artifacts", ()))
    return SourceFacts(
        n, MappingProxyType(sources), MappingProxyType(records), MappingProxyType(queries), last,
        MappingProxyType(expired), frozenset(artifacts),
    )  # fmt: skip


def read_inputs(view: LogView, kind: str) -> Mapping[str, str]:
    """Every input of ``kind`` the owner recorded a read of, to its newest version read."""
    newest: dict[str, str] = {}
    for e in view.events:
        if e.type == "read.recorded":
            read = InputRef.from_json(e.payload["input"])
            if read.kind == kind:
                newest[read.id] = read.version
    return MappingProxyType(newest)


def record_key(read: InputRef) -> str:
    """The record ID ``source.changed`` keeps ``read`` under: a time read's is ``time/<id>``."""
    return f"{CLOCK_SOURCE}/{read.id}" if read.kind == "time" else read.id


def record_source_changes(
    view: LogView,
    sources: Iterable[VersionedSource],
    *,
    cause: SourceCause,
    run_query: QueryRunner | None = None,
    change_set: str | None = None,
    expired: Iterable[InputRef] = (),
) -> tuple[NewEvent, ...]:
    """The source facts for ``view``'s owner (module doc): what moved, then what expired.

    Returns nothing when no source moved and nothing expired. Only reads the sources;
    never appends.
    """
    owner = view.owner
    facts = source_facts(view)
    read = read_inputs(view, "record")
    queried = run_query is not None and bool(read_inputs(view, "query") or facts.queries)
    unseen: dict[str, str] = {}  # never recorded, no read record moved: queries decide
    moved_sources: dict[str, str] = {}
    moved_records: dict[str, str | None] = {}
    checks: dict[str, SpanCheck] = {}
    index = None
    for source in sources:
        now = source.version(owner)
        prior = facts.sources.get(source.name)
        if prior == now:
            continue
        changed = _moved_records(source, owner, read, facts.records)
        if source.name == CLOCK_SOURCE:
            changed = {CLOCK_RECORD: source.record_version(owner, CLOCK_RECORD)}
        if prior is not None or changed:
            moved_sources[source.name] = now
            moved_records.update(changed)
            if changed and isinstance(source, RecordTexts):
                index = index or span_index(view)
                checks.update(recheck(index, source.name, changed, _text_of(source)))
        elif queried and cause == "command" and source.name != CLOCK_SOURCE:
            unseen[source.name] = now
    number = facts.source_version
    events: list[NewEvent] = []
    hashes: dict[str, str] | None = None
    if unseen and not moved_sources:
        assert run_query is not None
        hashes = {q: run_query(owner, q) for q in _queries(view, facts)}
        if any(hashes[q] != prev for q, prev in _queries(view, facts).items()):
            moved_sources.update(unseen)
    elif unseen:
        moved_sources.update(unseen)
    if moved_sources:
        number += 1
        events.extend(_changed(view, number, cause, moved_sources, moved_records, change_set,
                               run_query, facts, hashes))  # fmt: skip
        rechecked = rechecked_event(number, checks)
        if rechecked is not None:
            events.append(rechecked)
    gone = sorted({r.id for r in expired if r.kind == "record"})
    versions = sorted({f"{r.id}@{r.version}" for r in expired if r.kind == "artifact"})
    if gone or versions:
        number += 1
        payload: dict[str, object] = {"source_version": number, "cause": cause,
                                      "records": gone, "artifacts": versions}  # fmt: skip
        events.append(NewEvent(SOURCE_EXPIRED, payload))
    return tuple(events)


def _changed(
    view: LogView,
    number: int,
    cause: SourceCause,
    moved_sources: Mapping[str, str],
    moved_records: Mapping[str, str | None],
    change_set: str | None,
    run_query: QueryRunner | None,
    facts: SourceFacts,
    hashes: Mapping[str, str] | None = None,
) -> list[NewEvent]:
    owner = view.owner
    payload: dict[str, object] = {
        "source_version": number,
        "cause": cause,
        "sources": dict(sorted(moved_sources.items())),
        "records": dict(sorted(moved_records.items())),
    }
    if change_set is not None:
        payload["change_set"] = change_set
    events = [NewEvent("source.changed", payload)]
    if run_query is not None:
        for query, previous in sorted(_queries(view, facts).items()):
            new = hashes[query] if hashes is not None else run_query(owner, query)
            events.append(
                NewEvent(
                    "query.reexecuted",
                    {
                        "query": query,
                        "source_version": number,
                        "previous": previous,
                        "hash": new,
                        "changed": new != previous,
                    },
                )
            )
    return events


def _queries(view: LogView, facts: SourceFacts) -> dict[str, str]:
    """Every recorded query of the owner to the result-set hash last known for it."""
    return {**read_inputs(view, "query"), **facts.queries}


def _text_of(source: RecordTexts) -> Callable[[str], str | None]:
    name = str(getattr(source, "name", ""))

    def text(record_id: str) -> str | None:
        found = source.read(name, record_id)
        return None if found is None else found.text

    return text


def _moved_records(
    source: VersionedSource,
    owner: str,
    read: Mapping[str, str],
    known: Mapping[str, str | None],
) -> dict[str, str | None]:
    """The records ``owner`` read from ``source`` whose version is not the one last known."""
    collections = frozenset(source.collections)
    out: dict[str, str | None] = {}
    for record_id, read_version in sorted(read.items()):
        if record_id.split("/", 1)[0] not in collections:
            continue
        last = known.get(record_id, read_version)
        now = source.record_version(owner, record_id)
        if now != last:
            out[record_id] = now
    return out
