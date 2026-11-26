"""External record state (ADR-P29): the outside records Aksha's effects created or revised.

An effect whose tool declares ``EffectSpec.record_fields`` names one outside record (D1):
requests with equal record identity are revisions of one record, whatever their request
identity (``key_fields``). ``enqueue`` records that identity and the artifact versions
among the key values on ``outbox.enqueued``; nothing else is stored.

``ExternalRecords`` reduces the ``outbox.*`` events into one record per (owner, tool,
record identity) (D2): each completion is a revision, in completion order, with the
system's receipt; an entry with no outcome yet is ``pending``; an entry abandoned
``outcome_unknown`` is ``unknown``, since the outside system may hold a revision Aksha
never recorded. A refused, stopped or retries-exhausted entry is no revision.

:func:`external_records` joins each record's freshness at read time (D3): the state, in
the invalidation view (ADR-P05), of the version its latest revision was written from. So
an interface can show "the forms service holds v1, which Aksha has invalidated" without
writing a mark. Marking the outside copy superseded is the agent's own declared effect
through the outbox, never an automatic write (D4); nothing here calls a system.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from aksha.runtime.artifacts.types import ArtifactRef
from aksha.runtime.events.envelope import Event
from aksha.runtime.invalidation.types import STALE_STATES, Freshness
from aksha.runtime.invalidation.view import InvalidationView, invalidation_view
from aksha.runtime.outbox import facts
from aksha.runtime.outbox.keys import record_key
from aksha.runtime.outbox.types import OUTCOME_UNKNOWN, SystemReceipt
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.view import ArtifactsView, LogView

__all__ = [
    "EXTERNAL_RECORDS",
    "ExternalRecord",
    "ExternalRecords",
    "external_records",
    "record_key",
]

_ORDER: Mapping[Freshness, int] = MappingProxyType(
    {"current": 0, "current_by_cutoff": 1, "dirty": 2, "invalidated": 3}
)
"""How stale each state is; a record with several sources reads its stalest."""


@dataclass(frozen=True)
class ExternalRecord:
    """One outside record, as the outbox facts left it; a projection, never stored.

    ``record_key`` is ``sha256`` over the owner, the tool and the record-field values,
    ``fields`` those values. ``revisions`` are the system's receipts, oldest first, and
    ``sources`` the artifact versions the latest revision was written from (first:
    ``source_artifact``). ``freshness`` is the stalest of their states in the
    invalidation view, or None when no revision names a saved version. ``pending`` and
    ``unknown`` hold the idempotency keys of revisions not yet answered and of ones whose
    outcome nobody can know.
    """

    system: str
    record_key: str
    tool: str
    fields: Mapping[str, Any]
    revisions: tuple[SystemReceipt, ...]
    sources: tuple[ArtifactRef, ...] = ()
    freshness: Freshness | None = None
    pending: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()

    @property
    def source_artifact(self) -> ArtifactRef | None:
        """The version the latest revision was written from, or None."""
        return self.sources[0] if self.sources else None

    @property
    def stale(self) -> bool:
        """Whether the outside system holds a version Aksha has invalidated or dirtied."""
        return self.freshness in STALE_STATES


@dataclass(frozen=True)
class _Record:
    """A record as the reducer builds it, before the freshness join."""

    system: str
    tool: str
    fields: Mapping[str, Any]
    revisions: tuple[SystemReceipt, ...] = ()
    sources: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()


@dataclass(frozen=True)
class _State:
    """Each tracked entry's record key and sources, and each record by key."""

    entries: Mapping[str, tuple[str, tuple[str, ...]]]
    records: Mapping[str, _Record]


class ExternalRecords:
    """Reducer over the ``outbox.*`` events: one record per record identity (D2)."""

    name = "external_records"
    reads = facts.TYPES

    def initial(self, args: Hashable) -> _State:
        """No records."""
        return _State(MappingProxyType({}), MappingProxyType({}))

    def apply(self, state: _State, event: Event, artifacts: ArtifactsView) -> _State:
        """Track an entry that names a record; move it to its record's revisions or ends."""
        p = event.payload
        key = str(p["key"])
        if event.type == "outbox.enqueued":
            record = p.get("record")
            if record is None:
                return state
            rkey = str(record["key"])
            sources = tuple(str(s) for s in p.get("sources", ()))
            target = p["system"] if p["system"] is not None else p["tool"]
            found = state.records.get(rkey) or _Record(
                str(target), str(p["tool"]), record["fields"]
            )
            found = replace(found, pending=(*found.pending, key))
            return _State(
                MappingProxyType({**state.entries, key: (rkey, sources)}),
                MappingProxyType({**state.records, rkey: found}),
            )
        tracked = state.entries.get(key)
        if tracked is None or event.type not in ("outbox.completed", "outbox.abandoned"):
            return state
        rkey, sources = tracked
        rec = state.records[rkey]
        pending = tuple(k for k in rec.pending if k != key)
        if event.type == "outbox.completed":
            receipt = SystemReceipt(key, str(p["ledger_id"]), p["receipt"])
            rec = replace(rec, revisions=(*rec.revisions, receipt), sources=sources,
                          pending=pending)  # fmt: skip
        elif p["reason"] == OUTCOME_UNKNOWN:
            rec = replace(rec, pending=pending, unknown=(*rec.unknown, key))
        else:
            rec = replace(rec, pending=pending)
        return _State(state.entries, MappingProxyType({**state.records, rkey: rec}))


EXTERNAL_RECORDS = ExternalRecords()
"""The one instance the runtime uses."""


def external_records(view: LogView, system: str | None = None) -> tuple[ExternalRecord, ...]:
    """The view's owner's outside records, in first-enqueue order, with freshness joined.

    A record is listed once a revision completed or an outcome is unknown: a record with
    only pending revisions has not reached the outside system yet. ``system`` keeps the
    records of one system (an entry's ``system``, or its tool's name when it has none).
    Pure: nothing is appended and no system is called.
    """
    state = rebuild(EXTERNAL_RECORDS, view)
    chosen = [
        (k, r)
        for k, r in state.records.items()
        if (r.revisions or r.unknown) and (system is None or r.system == system)
    ]
    if not chosen:
        return ()
    iv = invalidation_view(view)
    return tuple(_joined(k, r, iv) for k, r in chosen)


def _joined(rkey: str, rec: _Record, iv: InvalidationView) -> ExternalRecord:
    refs = tuple(ArtifactRef.parse(s) for s in rec.sources)
    return ExternalRecord(
        system=rec.system,
        record_key=rkey,
        tool=rec.tool,
        fields=rec.fields,
        revisions=rec.revisions,
        sources=refs,
        freshness=_stalest(iv.versions[s].freshness for s in rec.sources if s in iv.versions),
        pending=rec.pending,
        unknown=rec.unknown,
    )


def _stalest(states: Iterable[Freshness]) -> Freshness | None:
    stalest: Freshness | None = None
    for state in states:
        if stalest is None or _ORDER[state] > _ORDER[stalest]:
            stalest = state
    return stalest
