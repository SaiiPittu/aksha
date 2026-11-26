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
4. ``rebuild_key`` is ``rebuild:{owner}/{agent}/{source_version}/{entries}``, where
   ``entries`` is a short hash of the invalidated versions the run is opened for: one
   rebuild run per agent per set of runnable work. Amendment from the build: a
   ``run.started`` has one dedup template (``rstart:{run}``), so the key is a payload
   field and the single writer checks it before appending, rather than a second
   store-enforced key. The entry set is in the key because work can become runnable with
   no new source change (a reader held behind a review becomes runnable when the review is
   accepted after the rebuild run ended); a key of the source version alone would never
   open a run for it. The same set is never opened twice, so a rebuild run that failed
   or was stopped is not retried in a loop; new work (another change, a person's
   decision) is a new set.
6. Headless (decision 26): a rebuild run is headless when a run it rebuilds for was
   (``for_runs``), and records ``headless`` on its ``run.started`` like any start. A
   headless start with no available sandbox backend is refused
   (``policy.headless.headless_start_refusal``): no run is opened for that agent.
5. Never for held work (TC-4, ``inv.no_auto_start_while_held``, narrowed by ADR-P16 S1):
   each entry asks ``stop_policy.hold.rebuild_blocked``, which holds it while the agent
   is held by a person's stop or the writer's own instance is held. An agent with an open
   run is not opened again; its run regenerates the work in place.

7. Re-entry setting (decision 36): ``run.started{reentry}`` says whether the run's
   re-entries may resume their writer's session; the opener names it, or the run
   inherits ``resume`` only when every run it rebuilds for chose ``resume``. Fresh is
   the default.
8. Modes (decision 38): only versions of runs whose agent declared ``rebuild`` are ever
   invalidated (``invalidation.view`` rule 6), so only such agents get a rebuild run, and
   the run records ``invalidation: rebuild`` on its ``run.started``, and the keying of
   the runs it rebuilds for (ADR-P05 M2): ``citations`` only when every one keyed on
   citations, else ``reads``.

Decision 34 chose a fresh context with a rebuild summary for the first milestone;
:func:`rebuild_brief` builds it. Resume on the existing session (decision 36) is
``context.reentry``; fresh stays the default until it is measured.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from aksha.runtime.artifacts.types import ArtifactRef, InputRef
from aksha.runtime.artifacts.versions import version
from aksha.runtime.canonical import sha256_hex
from aksha.runtime.context.reentry import ReentrySetting, inherited_setting
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.ports import IdSource
from aksha.runtime.invalidation.keying import inherited_keys
from aksha.runtime.invalidation.plan import rebuild_plan, runnable
from aksha.runtime.invalidation.types import EXPIRED, ReadStatus, RebuildBrief, RebuildEntry
from aksha.runtime.invalidation.view import (
    InvalidationView,
    invalidation_view,
    is_expiry_read,
)
from aksha.runtime.policy.headless import headless_start_refusal, started_headless
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS, open_runs
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.sandbox.types import Availability
from aksha.runtime.stop_policy.hold import rebuild_blocked
from aksha.runtime.storage.ports import EventStore

REBUILD_BOUNDARY = "U9"
REBUILD_CAUSE = "rebuild"


def rebuild_key(owner: str, agent: str, source_version: int, entries: Iterable[str] = ()) -> str:
    """The key of the one rebuild run per agent and set of runnable work (D8, rule 4)."""
    work = sha256_hex(sorted(set(entries)))[:12]
    return f"rebuild:{owner}/{agent}/{source_version}/{work}"


def rebuild_starts(
    view: LogView,
    ids: IdSource,
    *,
    definition_hashes: Mapping[str, str] | None = None,
    agents: Iterable[str] | None = None,
    sandbox: Availability | None = None,
    reentry: ReentrySetting | None = None,
    budgets: Mapping[str, Mapping[str, int]] | None = None,
) -> tuple[NewEvent, ...]:
    """The ``run.started{cause: rebuild}`` facts the owner's log calls for now (module doc).

    Allocates run IDs from ``ids``; appends nothing. ``agents`` limits the opening to
    those agents (a loop that drives one agent); ``sandbox`` is the sandbox backend's
    availability, asked for a headless rebuild (rule 6). ``reentry`` is the re-entry
    setting the runs record (rule 7); None inherits it from the runs rebuilt for.
    ``budgets`` holds each agent's declared budget as a ``run.started`` records it: a
    rebuild run of that agent records it too, so it is metered like the agent's request
    runs, not by the runtime's default.
    """
    only = None if agents is None else frozenset(agents)
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
        if only is not None and agent not in only:
            continue
        work = [str(e.invalidated) for e in mine]
        key = rebuild_key(view.owner, agent, iv.sources.source_version, work)
        if agent in busy or key in keys:
            continue
        for_runs = sorted({r for e in mine for r in iv.state(e.invalidated).line})
        headless = any(started_headless(view, r) for r in for_runs)
        if headless_start_refusal(headless, sandbox) is not None:
            continue
        payload: dict[str, object] = {
            "agent": agent,
            "cause": REBUILD_CAUSE,
            "for_runs": for_runs,
            "rebuild_key": key,
            "invalidation": "rebuild",
            "invalidation_keys": inherited_keys(view, for_runs),
        }
        if headless:
            payload["headless"] = True
        payload["reentry"] = reentry or inherited_setting(view, for_runs)
        if definition_hashes and agent in definition_hashes:
            payload["definition_hash"] = definition_hashes[agent]
        if budgets and agent in budgets:
            payload["budget"] = dict(budgets[agent])
        out.append(NewEvent("run.started", payload, run=ids.next(view.owner, "R")))
    return tuple(out)


def open_rebuild_runs(
    store: EventStore,
    owner: str,
    ids: IdSource,
    *,
    definition_hashes: Mapping[str, str] | None = None,
    agents: Iterable[str] | None = None,
    sandbox: Availability | None = None,
    reentry: ReentrySetting | None = None,
    budgets: Mapping[str, Mapping[str, int]] | None = None,
) -> tuple[str, ...]:
    """Open the rebuild runs the owner's log calls for; return their run IDs (module doc).

    Called after every commit that can change the plan: by recovery (its
    ``rebuild_check``) and by ``loop.drive.drive_rebuilds``, which then drives each run.
    """
    starts = rebuild_starts(
        load_view(store, owner), ids, definition_hashes=definition_hashes, agents=agents,
        sandbox=sandbox, reentry=reentry, budgets=budgets,
    )  # fmt: skip
    for start in starts:
        store.append(owner, REBUILD_BOUNDARY, (start,))
    return tuple(str(s.run) for s in starts)


def rebuild_brief(view: LogView, iv: InvalidationView, entry: RebuildEntry) -> RebuildBrief:
    """The fresh context a rebuilt instance starts from (D10): old version, changes, reads."""
    old = version(view, entry.invalidated)
    if old is None:
        raise KeyError(f"{entry.invalidated} is not in {view.owner}'s log")
    state = iv.state(entry.invalidated)
    # An expired version's ``time:expiry`` marker says why it is stale; it is not a read.
    current = tuple(
        _now(r) for r in state.reads if r.state != "gone" and not is_expiry_read(r.input)
    )
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
    """The input a rebuilt instance reads in place of ``status.input``.

    An expired read (ADR-P25 D3) names no newer version: the instance reads it again.
    """
    read = status.input
    if status.now is None or status.now == EXPIRED or status.state in ("same", "unsettled"):
        return read
    if read.kind == "artifact":
        return ArtifactRef.parse(status.now).as_input()
    return InputRef(read.kind, read.id, status.now)
