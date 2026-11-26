"""What a source change notices (ADR-P05 amendment M2): ``evidence.noticed``, in its transaction.

A citation-keyed version whose read moved without moving its evidence is not reopened;
the change is noticed instead. :func:`notices_for` takes the events a source change is
about to commit (``source.changed``, ``query.reexecuted``, ``excerpts.rechecked``) and
returns one ``evidence.noticed`` per latest version of a citation-keyed run that a read of
it moved in this change:

1. ``rebuild`` mode: the reads the view marks ``weak`` give a ``weak`` notice, a "new
   evidence may exist" with a low-priority recheck item (``keying.recheck_items``). A
   version the change invalidates gets no notice: its rebuild is the answer.
2. ``notify`` mode: the version never reads as stale, so the notice is all it gets:
   ``strong`` (its cited evidence or a scope changed, a high-priority recheck) when any
   read that moved is strong, else ``weak``.

Read-keyed runs and runs in ``off`` or ``edit_guard`` mode get none. Pure.
"""

from __future__ import annotations

from collections.abc import Sequence

from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.artifacts.versions import all_versions
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.grounding.spans import span_index
from aksha.runtime.invalidation.confirm import after
from aksha.runtime.invalidation.keying import Keyed, Strength, notice_event
from aksha.runtime.invalidation.modes import UNRECORDED_MODE, run_modes
from aksha.runtime.invalidation.view import invalidation_view
from aksha.runtime.projections.view import LogView


def notices_for(view: LogView, events: Sequence[NewEvent]) -> tuple[NewEvent, ...]:
    """The ``evidence.noticed`` facts the source change ``events`` call for (module doc)."""
    changed = next((e for e in events if e.type == "source.changed"), None)
    if changed is None:
        return ()
    number = int(changed.payload["source_version"])
    records = {str(k): v for k, v in changed.payload["records"].items()}
    queries = {
        str(e.payload["query"]): str(e.payload["hash"])
        for e in events
        if e.type == "query.reexecuted" and e.payload["changed"]
    }
    future = after(view, events)
    keyed = Keyed.of(future, span_index(future))
    if "citations" not in keyed.keys.values():
        return ()
    modes = run_modes(future)
    iv = invalidation_view(future)
    out: list[NewEvent] = []
    for v in all_versions(future):
        state = iv.versions.get(str(v.ref))
        if state is None or not state.is_latest or not keyed.keyed_on_citations(v):
            continue
        mode = modes.get(v.run or "", UNRECORDED_MODE)
        if mode not in ("notify", "rebuild") or (mode == "rebuild" and state.stale):
            continue
        moved = [r for r in v.reads if _moved_now(r, records, queries)]
        if not moved:
            continue
        strength: Strength = "weak"
        if mode == "notify" and any(keyed.strength(v, r) == "strong" for r in moved):
            strength = "strong"
        elif mode == "rebuild":
            weak = {r.input for r in state.weak_reads()}
            moved = [r for r in moved if r in weak]
            if not moved:
                continue
        out.append(notice_event(str(v.ref), strength, moved, number, v.run))
    return tuple(out)


def _moved_now(read: InputRef, records: dict[str, object], queries: dict[str, str]) -> bool:
    """Whether this change moved ``read``: its record (or part) or its query's hit set."""
    if read.kind == "record" and read.id in records:
        return records[read.id] != read.version
    if read.kind == "query" and read.id in queries:
        return queries[read.id] != read.version
    return False
