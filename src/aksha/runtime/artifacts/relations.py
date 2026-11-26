"""Typed relations: ``relation_saved`` facts, the ``relations`` index and ``components``.

ADR-P04 D2, D12 and S2. A typed relation is a claim an agent declares (``same_as``,
``contradicts``) between artifact versions or records, saved with at least one excerpt as
its evidence and carried by the version whose save asked for it. A relation is current
exactly while its carrier is the latest version of the carrier's slot: a newer carrier
version that no longer asks for it retracts it, with nothing deleted.

``components`` (S2) groups the ends joined by one relation kind, treating each relation as
undirected, so an agent counts entities rather than hits without writing union-find.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.artifacts.blobs import inflate
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.types import ArtifactRef, Excerpt, InputRef, Relation
from aksha.runtime.artifacts.versions import latest
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.projections.view import LogView


def relation_saved(
    kind: str,
    label: str | None,
    src: InputRef,
    dst: InputRef,
    evidence: tuple[Excerpt, ...],
    carrier: ArtifactRef,
    n: int,
    *,
    run: str | None,
    instance: str | None,
    step: str | None,
) -> NewEvent:
    """The ``relation.saved`` fact of the ``n``-th relation ``carrier``'s save asked for."""
    payload: Mapping[str, Any] = {
        "kind": kind,
        "label": label,
        "src": src.as_json(),
        "dst": dst.as_json(),
        "evidence": [x.as_json() for x in evidence],
        "carrier": str(carrier),
        "n": n,
    }
    return NewEvent("relation.saved", payload, run=run, instance=instance, step=step)


def _relation(event: Event, blobs: BlobStore | None) -> Relation:
    p = inflate(event.payload, blobs)
    return Relation(
        kind=str(p["kind"]),
        label=p["label"],
        src=InputRef.from_json(p["src"]),
        dst=InputRef.from_json(p["dst"]),
        evidence=tuple(Excerpt.from_json(x) for x in p["evidence"]),
        carrier=ArtifactRef.parse(str(p["carrier"])),
        event_seq=event.seq,
    )


def _touches(rel: Relation, ref: InputRef) -> bool:
    return any(end.same_source(ref) and end.version == ref.version for end in (rel.src, rel.dst))


def relations(
    view: LogView,
    *,
    kind: str | None = None,
    touching: ArtifactRef | InputRef | None = None,
    carrier: ArtifactRef | None = None,
    current_only: bool = True,
    blobs: BlobStore | None = None,
) -> tuple[Relation, ...]:
    """The saved relations, in commit order, filtered by kind, an end, or their carrier.

    With ``current_only`` (the default) only relations whose carrier is its slot's latest
    version are returned.
    """
    end = touching.as_input() if isinstance(touching, ArtifactRef) else touching
    out: list[Relation] = []
    latest_of: dict[tuple[str, str | None], int | None] = {}
    for e in view.events:
        if e.type != "relation.saved" or (kind is not None and e.payload.get("kind") != kind):
            continue
        rel = _relation(e, blobs)
        if carrier is not None and rel.carrier != carrier:
            continue
        if end is not None and not _touches(rel, end):
            continue
        if current_only:
            slot = rel.carrier.slot()
            if slot not in latest_of:
                newest = latest(view, *slot, blobs=blobs)
                latest_of[slot] = None if newest is None else newest.ref.version
            if latest_of[slot] != rel.carrier.version:
                continue
        out.append(rel)
    return tuple(out)


def components(
    view: LogView, kind: str, *, blobs: BlobStore | None = None
) -> tuple[frozenset[InputRef], ...]:
    """The groups of ends the current relations of ``kind`` join, largest first (S2).

    Each relation joins its two ends whatever its direction. Groups are sorted by size,
    then by their smallest member, so the order is the same on every replay.
    """
    parent: dict[InputRef, InputRef] = {}

    def find(x: InputRef) -> InputRef:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for rel in relations(view, kind=kind, blobs=blobs):
        a, b = find(rel.src), find(rel.dst)
        if a != b:
            parent[max(a, b, key=InputRef.sort_key)] = min(a, b, key=InputRef.sort_key)
    groups: dict[InputRef, set[InputRef]] = {}
    for x in list(parent):
        groups.setdefault(find(x), set()).add(x)
    found = [frozenset(g) for g in groups.values()]
    return tuple(sorted(found, key=lambda g: (-len(g), min(r.sort_key() for r in g))))
