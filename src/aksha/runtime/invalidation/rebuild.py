"""Rebuild runs (ADR-P05 D8, S1) and the fresh context of a rebuilt instance (D10).

When the invalidation view changes for an owner and an affected agent has no open run,
:func:`open_rebuild_runs` opens one run for that agent and owner, cause ``rebuild``, in its
own boundary (U9). An agent with an open run regenerates its work in place instead. Rules:

1. Only runnable plan entries count: an agent whose every entry is held behind an
   unsettled read gets no run yet, which is the dependency order across agents (an
   upstream agent rebuilds before an agent that read its work).
2. Agents are opened oldest entry first.
3. ``for_runs`` names the runs whose invalidated work the run regenerates (S1); its
   versions are written for those runs, which decides whose readers they may move.
4. ``rebuild_key`` is ``rebuild:{owner}/{agent}/{source_version}``: one rebuild run per
   agent per source change. Amendment from the build: a ``run.started`` has one dedup
   template (``rstart:{run}``), so the key is a payload field and the single writer
   checks it before appending, rather than a second store-enforced key.
5. Never for held work (TC-4, ``inv.no_auto_start_while_held``, narrowed by ADR-P16 S1):
   each entry asks ``stop_policy.hold.rebuild_blocked``, which holds it while the agent
   is held by a person's stop or the writer's own instance is held. An agent with an open
   run is not opened again; its run regenerates the work in place.

Decision 34 chose a fresh context with a rebuild brief for the first milestone;
:func:`rebuild_brief` is that brief. Resume with masking (decision 36) comes next.
"""

from __future__ import annotations

from collections.abc import Mapping

from aksha.runtime.artifacts.types import ArtifactRef, InputRef
from aksha.runtime.artifacts.versions import version
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.ports import IdSource
from aksha.runtime.invalidation.plan import rebuild_plan, runnable
from aksha.runtime.invalidation.types import ReadStatus, RebuildBrief, RebuildEntry
from aksha.runtime.invalidation.view import InvalidationView, invalidation_view
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS, open_runs
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.stop_policy.hold import rebuild_blocked
from aksha.runtime.storage.ports import EventStore

REBUILD_BOUNDARY = "U9"
REBUILD_CAUSE = "rebuild"


def rebuild_key(owner: str, agent: str, source_version: int) -> str:
    """The key of the one rebuild run per agent and source change (D8)."""
    return f"rebuild:{owner}/{agent}/{source_version}"


def rebuild_starts(
    view: LogView, ids: IdSource, *, definition_hashes: Mapping[str, str] | None = None
) -> tuple[NewEvent, ...]:
    """The ``run.started{cause: rebuild}`` facts the owner's log calls for now (module doc).

    Allocates run IDs from ``ids``; appends nothing.
    """
    iv = invalidation_view(view)
    entries = tuple(e for e in runnable(rebuild_plan(iv)) if rebuild_blocked(view, e) is None)
    if not entries:
        return ()
    runs = rebuild(RUNS, view)
    busy = {runs[r].agent for r in open_runs(runs)}
    keys = {
        str(e.payload.get("rebuild_key"))
        for e in view.events
        if e.type == "run.started" and e.payload.get("cause") == REBUILD_CAUSE
    }
    by_agent: dict[str, list[RebuildEntry]] = {}
    for entry in entries:  # oldest first, so agents keep their first entry's order
        by_agent.setdefault(entry.writer.agent, []).append(entry)
    out: list[NewEvent] = []
    for agent, mine in by_agent.items():
        key = rebuild_key(view.owner, agent, iv.sources.source_version)
        if agent in busy or key in keys:
            continue
        for_runs = sorted({r for e in mine for r in iv.state(e.invalidated).line})
        payload: dict[str, object] = {
            "agent": agent,
            "cause": REBUILD_CAUSE,
            "for_runs": for_runs,
            "rebuild_key": key,
        }
        if definition_hashes and agent in definition_hashes:
            payload["definition_hash"] = definition_hashes[agent]
        out.append(NewEvent("run.started", payload, run=ids.next(view.owner, "R")))
    return tuple(out)


def open_rebuild_runs(
    store: EventStore,
    owner: str,
    ids: IdSource,
    *,
    definition_hashes: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Open the rebuild runs the owner's log calls for; return their run IDs (module doc)."""
    starts = rebuild_starts(load_view(store, owner), ids, definition_hashes=definition_hashes)
    for start in starts:
        store.append(owner, REBUILD_BOUNDARY, (start,))
    return tuple(str(s.run) for s in starts)


def rebuild_brief(view: LogView, iv: InvalidationView, entry: RebuildEntry) -> RebuildBrief:
    """The fresh context a rebuilt instance starts from (D10): old version, changes, reads."""
    old = version(view, entry.invalidated)
    if old is None:
        raise KeyError(f"{entry.invalidated} is not in {view.owner}'s log")
    state = iv.state(entry.invalidated)
    current = tuple(_now(r) for r in state.reads if r.state != "gone")
    statements = tuple(r for r in old.reads if r.kind == "statement")
    return RebuildBrief(
        old=old.ref,
        content=old.content,
        changed=state.changed_reads(),
        current_reads=current,
        statements=statements,
        via=state.via,
    )


def _now(status: ReadStatus) -> InputRef:
    """The input a rebuilt instance reads in place of ``status.input``."""
    read = status.input
    if status.now is None or status.state in ("same", "unsettled"):
        return read
    if read.kind == "artifact":
        return ArtifactRef.parse(status.now).as_input()
    return InputRef(read.kind, read.id, status.now)
