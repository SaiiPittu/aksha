"""Source facts: the one writer of ``source.changed`` and ``query.reexecuted`` (ADR-P05 S2, D6).

``record_source_changes`` is the only function that builds either fact. The
``apply_source_change`` command and recovery's source check both call it, once per owner,
and commit what it returns in the source-change boundary (U8), with the revocations the
change confirms (D9). It asks each ``VersionedSource`` for the owner's versions, compares
them with the versions the owner's log last knew, and records only what moved:

1. A source whose version differs from the last recorded one (or that was never recorded)
   is checked record by record, for every record the owner has read from it. A source
   never recorded before is recorded when one of its read records moved, or when the
   owner has recorded queries to re-execute: a file no work read yet can still change
   what a listing or a search returns (amendment from the ingestion build, 26 Nov 2026).
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
5. ``expired`` (D3) lists the records and artifact versions whose freshness lifetime ran
   out: one ``source.expired`` fact names them, counted as a source change.

``source_facts`` is the projection over those facts that the invalidation view reads.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.invalidation.ports import QueryRunner, VersionedSource
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
    moved_sources: dict[str, str] = {}
    moved_records: dict[str, str | None] = {}
    for source in sources:
        now = source.version(owner)
        prior = facts.sources.get(source.name)
        if prior == now:
            continue
        changed = _moved_records(source, owner, read, facts.records)
        if source.name == CLOCK_SOURCE:
            changed = {CLOCK_RECORD: source.record_version(owner, CLOCK_RECORD)}
        if prior is not None or changed or (queried and source.name != CLOCK_SOURCE):
            moved_sources[source.name] = now
            moved_records.update(changed)
    number = facts.source_version
    events: list[NewEvent] = []
    if moved_sources:
        number += 1
        events.extend(_changed(view, number, cause, moved_sources, moved_records, change_set,
                               run_query, facts))  # fmt: skip
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
        queries = {**read_inputs(view, "query"), **facts.queries}
        for query, previous in sorted(queries.items()):
            new = run_query(owner, query)
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
