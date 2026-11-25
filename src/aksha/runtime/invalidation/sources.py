"""Source facts: the one writer of ``source.changed`` and ``query.reexecuted`` (ADR-P05 S2, D6).

``record_source_changes`` is the only function that builds either fact. The
``apply_source_change`` command and recovery's source check both call it, once per owner,
and commit what it returns in the source-change boundary (U8), with the revocations the
change confirms (D9). It asks each ``VersionedSource`` for the owner's versions, compares
them with the versions the owner's log last knew, and records only what moved:

1. A source whose version differs from the last recorded one (or that was never recorded)
   is checked record by record, for every record the owner has read from it.
2. A record whose version differs from the one last known (recorded by a change, or else
   the newest read of it) is recorded with its new version, or None when it is gone.
3. Only when something is recorded are the owner's recorded queries re-executed, each
   writing ``query.reexecuted`` with its new result-set hash and whether it ``changed``.
   An unchanged source re-executes nothing.

``source_facts`` is the projection over those facts that the invalidation view reads.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.invalidation.ports import QueryRunner, VersionedSource
from aksha.runtime.invalidation.types import SourceCause, SourceFacts
from aksha.runtime.projections.view import LogView

SOURCE_CHANGE_BOUNDARY = "U8"


def source_facts(view: LogView) -> SourceFacts:
    """The owner's source, record and query versions as its log last recorded them."""
    n, last = 0, 0
    sources: dict[str, str] = {}
    records: dict[str, str | None] = {}
    queries: dict[str, str] = {}
    for e in view.events:
        p = e.payload
        if e.type == "source.changed":
            n, last = int(p["source_version"]), e.seq
            sources.update({str(k): str(v) for k, v in p["sources"].items()})
            records.update({str(k): None if v is None else str(v) for k, v in p["records"].items()})
        elif e.type == "query.reexecuted":
            queries[str(p["query"])] = str(p["hash"])
    return SourceFacts(
        n, MappingProxyType(sources), MappingProxyType(records), MappingProxyType(queries), last
    )


def read_inputs(view: LogView, kind: str) -> Mapping[str, str]:
    """Every input of ``kind`` the owner recorded a read of, to its newest version read."""
    newest: dict[str, str] = {}
    for e in view.events:
        if e.type == "read.recorded":
            read = InputRef.from_json(e.payload["input"])
            if read.kind == kind:
                newest[read.id] = read.version
    return MappingProxyType(newest)


def record_source_changes(
    view: LogView,
    sources: Iterable[VersionedSource],
    *,
    cause: SourceCause,
    run_query: QueryRunner | None = None,
    change_set: str | None = None,
) -> tuple[NewEvent, ...]:
    """The ``source.changed`` and ``query.reexecuted`` facts for ``view``'s owner (module doc).

    Returns nothing when no source moved. Only reads the sources; never appends.
    """
    owner = view.owner
    facts = source_facts(view)
    read = read_inputs(view, "record")
    moved_sources: dict[str, str] = {}
    moved_records: dict[str, str | None] = {}
    for source in sources:
        now = source.version(owner)
        prior = facts.sources.get(source.name)
        if prior == now:
            continue
        changed = _moved_records(source, owner, read, facts.records)
        if prior is not None or changed:
            moved_sources[source.name] = now
            moved_records.update(changed)
    if not moved_sources:
        return ()
    number = facts.source_version + 1
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
    return tuple(events)


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
