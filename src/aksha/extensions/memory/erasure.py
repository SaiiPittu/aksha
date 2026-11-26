"""Lineage closure and the erasure hook (ADR-P09 D4, D9; ADR-P30).

``used_by`` answers "which outputs used this preference?": every artifact version whose
recorded lineage reaches the version, directly or through other versions. It reads only
``artifact.saved`` facts, so the answer is checked against the log, not kept beside it.

``forget`` only hides a record from recall: the event store is append-only, so the text
stays in the record's earlier versions, in the recall results that showed it and in the
model steps that read them. Real erasure waits on ADR-P30, which is open (erasable content
table, per-tenant keys, or no erasure). ``erasure_inventory`` is the hook that decision
plugs into: everything an erasure of one record would have to reach, found from the log
alone, so whichever option is chosen has a complete list to act on. It deletes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from aksha.runtime.api import LogView


@dataclass(frozen=True)
class ErasureInventory:
    """What erasing one memory record would have to reach (module doc).

    ``versions`` are every version of the slot, tombstones included; ``texts`` the distinct
    texts they held; ``events`` the ``(seq, type)`` of every event whose payload holds one
    of those texts (a recall result, a model step that read it, a summary); ``dependents``
    the artifact versions whose lineage reaches any version (``used_by``), which carry
    what the record said into their own content.
    """

    slot: str
    versions: tuple[str, ...]
    texts: tuple[str, ...]
    events: tuple[tuple[int, str], ...]
    dependents: tuple[str, ...]


def used_by(view: LogView, refs: Iterable[str]) -> tuple[str, ...]:
    """Every version whose lineage reaches one of ``refs``, in commit order (D4)."""
    reached = set(refs)
    out: list[str] = []
    for e in view.events:
        if e.type != "artifact.saved":
            continue
        ref = str(e.payload.get("ref"))
        if ref in reached:
            continue
        if any(_artifact_read(r) in reached for r in e.payload.get("reads", ())):
            reached.add(ref)
            out.append(ref)
    return tuple(out)


def erasure_inventory(view: LogView, kind: str, subject: str) -> ErasureInventory:
    """Everything an erasure of ``kind/subject`` would reach in ``view`` (module doc)."""
    slot = f"{kind}/{subject}"
    versions: list[str] = []
    texts: list[str] = []
    for e in view.events:
        if e.type == "artifact.saved" and e.payload.get("kind") == kind:
            if e.payload.get("key") != subject:
                continue
            ref = str(e.payload["ref"])
            versions.append(ref)
            row = view.artifacts.get(ref)
            text = None if row is None else row.row.content.get("text")
            if isinstance(text, str) and text not in texts:
                texts.append(text)
    needles = [json.dumps(t, ensure_ascii=False)[1:-1] for t in texts]
    events = tuple(
        (e.seq, e.type)
        for e in view.events
        if needles and any(n in _text(e.payload) for n in needles)
    )
    return ErasureInventory(slot, tuple(versions), tuple(texts), events, used_by(view, versions))


def _artifact_read(read: Mapping[str, Any]) -> str | None:
    if read.get("kind") != "artifact":
        return None
    return f"{read.get('id')}@{read.get('version')}"


def _text(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
