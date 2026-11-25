"""Recovery on startup and lazy resume (ADR-P02 D5, D6, D7, D10), and the projections they read.

``recover_on_startup`` runs before the process accepts a command. For every owner with an
open run (and every owner the caller names), in ADR-P02 D5's fixed order:

1. Reconcile the outbox entries that landed (ADR-P03 D7, ``OutboxWorker.reconcile
   ("landed")``): each claimed ``reconcile`` entry's system is asked by key, and a receipt
   it holds is recorded, never applied again.
2. The source check (ADR-P05 S2): a ``SourceCheck`` the composition root passes
   (``invalidation.commands.check_sources`` over the owner's source adapters) records
   what moved while the process was down, cause ``recovery``, through the one writer of
   ``source.changed``, with the revocations it confirms. Without one, nothing is checked.
3. Reconcile the entries that did not land (``reconcile("unlanded")``): a claimed
   ``at_most_once`` entry is closed ``outcome_unknown``; every other entry, claimed or
   only enqueued (a crash right after the deciding commit), is finished under its key if
   authority still holds, or abandoned with the authority's code. A tool step's entry
   ends together with its step's ``step.completed``. An owner with entries to finish and
   no outbox worker is refused (``effect_needs_outbox``): nothing is skipped silently.
4. Close each dispatched ``at_most_once`` step left dangling as ``step.completed
   {interrupted, outcome_unknown}``: it may have reached the outside world, and nothing
   can ask, so it is never run again (the replay policy for a body with no ledger).
5. Classify each open run (:func:`classify_open_run`): ``stop_requested`` first (a
   person's stop request is pending, ADR-P18 D3: it wins even over an open interrupt),
   then ``definition_changed`` (the loaded agent definition version hash differs from the
   one on ``run.started``), then ``suspended`` (an interrupt is open), then ``held`` (a
   person's stop of another run holds the agent, ADR-P18 D4), else ``in_flight``.
6. End a stop-requested run with the stop ending of its resume candidate (ADR-P18 D2:
   open interrupts voided, ``run.ended{stopped, stop_requested}`` and ``stop.decided``,
   one transaction, no model turn). End a ``definition_changed`` run the same way, with
   the Run frame's resume record (D7): its open interrupts voided
   (``interrupt.voided{definition_changed}``), ``run.ended{failed, definition_changed}``
   and ``stop.decided{verdict: stop, cause: resume, reason: definition_changed}``, so no
   person is left a question about a run that has ended. Write nothing at all for a
   suspended or held run; queue an in-flight run for its owner's next command.

The report lists, per owner, the steps closed unknown, the runs ended, the outbox keys
reconciled, and every ``effects_unknown`` step of an open run (a step the kill boundary
ended after its effects were authorised, ADR-P18 D6), which is how such a step is reported
until the interrupt for ``on_unknown="interrupt"`` (P17) is built.

``resume_owner`` drains one owner's in-flight runs (D10: lazily, before that owner's next
mutating command or on ``aksha resume``). It recovers the owner again first (all six
steps above). Each resume then writes a resume record, the stop decision
``stop.decided{cause: resume}`` (D7), judged by the restart limit (D8, ADR-P14 S2): with
``restart_limit`` resumes already recorded since the run's last progress (a completed tool
step or a saved artifact), the record is STOP and the run ends ``failed, restart_limit``
in the same transaction, never driven again. Otherwise the record re-authorises the
incomplete turn (no turn charged) and the run's driver is called again with a fresh
``StepExecutor`` (given the owner's outbox worker). Every step the run recorded returns
its recorded result, so the run continues at the first step with no recorded outcome. A
suspended or stopped run is never resumed, so it writes no resume record and its restarts
never count. Before each resume record the hold predicate is asked again (``stop_hold``,
ADR-P16 D6), so a stop committed since recovery holds the run too. When the driver
returns, the end of its turn is a stop candidate: a stop it reached is honoured there
(``run.ended{stopped}``).

Not built yet (slice-2 cuts):

1. ``RecoveryReport.prior_exit`` and ``rebuild_runs`` (rebuild runs are opened by
   ``invalidation.rebuild.open_rebuild_runs`` after recovery); ``recovery.completed``
   (U7); ``replay_check`` (D11).
2. Taking the lock and checking the schema version is the storage adapter's open, not a
   step here.
3. The rest of the stop-policy evaluation a resume record can hand over to (ADR-P16), and
   relabelling a lost turn end.
4. ``suspended_with_rebuilds`` (S1), which needs the invalidation view (P05).
5. The automatic-start limit: ``budgets.limits.auto_start_allowed`` exists, but
   ``resume_owner`` does not ask it before a resume record, although a resume record is an
   automatic start under ADR-P14 D7.
6. Owners: the store lists owners with an open run only, so an owner whose only pending
   work is a command's outbox entry (a handoff) is recovered on startup only when the
   caller names it, and otherwise by ``resume_owner`` before its next command.

``interrupt.raised``/``interrupt.resolved`` (P17) are read by name; no core type declares
them yet, so the suspended class is reached only from a stand-in type.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from aksha.runtime.budgets.limits import resumes_since_progress
from aksha.runtime.budgets.types import Limits
from aksha.runtime.cancellation.ending import honour_stop, stop_ending
from aksha.runtime.cancellation.hold import stop_hold
from aksha.runtime.cancellation.requests import pending_stop
from aksha.runtime.cancellation.types import STOP_REQUESTED
from aksha.runtime.definitions.hashing import resume_refusal
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution import facts
from aksha.runtime.execution.errors import EffectNeedsOutbox, NotRecoveryWriter
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.ports import Clock, RunDriver
from aksha.runtime.execution.types import STEP_SEMANTICS, DanglingStep, RunClass, StepSemantics
from aksha.runtime.interrupts.reducers import INTERRUPT_VOIDED, pending_interrupts
from aksha.runtime.outbox.outbox import pending_outbox
from aksha.runtime.outbox.worker import OutboxWorker
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS, RunSummary, open_runs
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.storage.errors import AlreadyRecorded
from aksha.runtime.storage.ports import EventStore

STOP_DECISION = "U3"
"""The boundary ``run.ended`` and a resume record are written in (a stop decision)."""

DEFAULT_LIMITS = Limits()
"""The runtime's limits, for a resume whose caller passes none."""

RESUME_CANDIDATE = "resume"
"""The stop candidate recovery writes for a run it ends or resumes (ADR-P02 D7)."""

TURN_END = "turn_end"
"""The stop candidate at the end of a turn a resumed driver returns from."""

DEFINITION_CHANGED = "definition_changed"
"""The reason of a run the Run frame ends because its agent definition changed (D7)."""

EFFECTS_UNKNOWN = "effects_unknown"
"""The outcome of a step the kill boundary ended after its effects were authorised."""


def dangling_steps(view: LogView, run: str | None = None) -> tuple[DanglingStep, ...]:
    """Steps with a ``step.started`` and no ``step.completed``, in start order. Pure."""
    started: dict[str, Event] = {}
    done: set[str] = set()
    dispatched: dict[str, int] = {}
    attempts: dict[str, int] = {}
    for e in view.events:
        if e.step is None or (run is not None and e.run != run):
            continue
        if e.type == "step.started":
            started.setdefault(e.step, e)
        elif e.type == "step.completed":
            done.add(e.step)
        elif e.type == "step.dispatched":
            dispatched[e.step] = int(e.payload["attempt"])
        elif e.type in ("step.failed", "attempt.discarded"):
            attempts[e.step] = max(attempts.get(e.step, 0), int(e.payload["attempt"]))
    out: list[DanglingStep] = []
    for sid, e in started.items():
        if sid in done:
            continue
        p = e.payload
        name = p.get("name", p.get("tool"))
        out.append(
            DanglingStep(
                step_id=sid,
                run=e.run,
                instance=e.instance,
                kind=str(p["kind"]),
                name=None if name is None else str(name),
                semantics=_semantics(str(p.get("semantics", "at_least_once"))),
                on_unknown="interrupt" if p.get("on_unknown") == "interrupt" else "tell_model",
                dispatched=sid in dispatched,
                attempts=max(attempts.get(sid, 0), dispatched.get(sid, 0)),
            )
        )
    return tuple(out)


def _semantics(name: str) -> StepSemantics:
    if name not in STEP_SEMANTICS:
        raise ValueError(f"a recorded step has semantics {name!r}")
    return name  # type: ignore[return-value]


def effects_unknown_steps(view: LogView, runs: Iterable[str]) -> tuple[str, ...]:
    """The ``effects_unknown`` steps of ``runs``, in commit order. Pure (ADR-P18 D6)."""
    wanted = set(runs)
    return tuple(
        str(e.step)
        for e in view.events
        if e.type == "step.completed"
        and e.payload.get("outcome") == EFFECTS_UNKNOWN
        and e.run in wanted
        and e.step is not None
    )


def changed_ending(view: LogView, run: str) -> tuple[NewEvent, ...]:
    """The Run frame's resume record for a run whose definition changed (D7). Pure.

    One transaction: each open interrupt voided, ``run.ended{failed, definition_changed}``,
    then the resume record ``stop.decided{verdict: stop}`` last.
    """
    voided = tuple(
        NewEvent(INTERRUPT_VOIDED, {"interrupt": i, "reason": DEFINITION_CHANGED}, run=run)
        for i in pending_interrupts(view, run)
    )
    ended = NewEvent("run.ended", {"outcome": "failed", "reason": DEFINITION_CHANGED}, run=run)
    walk = 1 + sum(1 for e in view.events if e.run == run and e.type == "stop.decided")
    record = facts.resume_record(run, walk, verdict="stop", reason=DEFINITION_CHANGED)
    return (*voided, ended, record)


def classify_open_run(view: LogView, run: str, loaded_hash: str | None) -> RunClass:
    """What recovery makes of open ``run`` when its agent loads as ``loaded_hash`` (D6). Pure.

    ``loaded_hash`` is None when the run's agent is not loaded at all, which is a change.
    """
    summary = rebuild(RUNS, view).get(run)
    if summary is None or summary.outcome is not None:
        raise ValueError(f"{run} is not an open run of {view.owner}")
    return _classify(view, summary, loaded_hash)


def _classify(view: LogView, summary: RunSummary, loaded_hash: str | None) -> RunClass:
    if pending_stop(view, summary.run) is not None:
        return "stop_requested"
    if loaded_hash is None or resume_refusal(summary.definition_hash, loaded_hash):
        return "definition_changed"
    if pending_interrupts(view, summary.run):
        return "suspended"
    if stop_hold(view, run=summary.run, agent=summary.agent) is not None:
        return "held"
    return "in_flight"


@dataclass(frozen=True)
class OwnerRecovery:
    """What recovery did for one owner."""

    owner: str
    closed_unknown: tuple[str, ...] = ()
    ended: tuple[tuple[str, str], ...] = ()
    suspended: tuple[str, ...] = ()
    in_flight: tuple[str, ...] = ()
    held: tuple[str, ...] = ()
    reconciled: tuple[str, ...] = ()
    effects_unknown: tuple[str, ...] = ()
    sources_moved: tuple[str, ...] = ()


SourceCheck = Callable[[EventStore, str], tuple[str, ...]]
"""Recovery's source check for one owner (ADR-P05 S2): records what moved, returns the sources."""


@dataclass(frozen=True)
class RecoveryReport:
    """What recovery on startup did, per owner, in owner order.

    ``closed_unknown`` lists ``(owner, step ID)``; ``ended`` lists ``(owner, run, reason)``;
    ``in_flight`` maps an owner to the runs waiting for its next command; ``reconciled``
    lists ``(owner, idempotency key)`` of every outbox entry recovery ended;
    ``effects_unknown`` lists ``(owner, step ID)`` of every ``effects_unknown`` step of
    an open run, so each is reported.
    """

    owners: tuple[OwnerRecovery, ...] = ()
    closed_unknown: tuple[tuple[str, str], ...] = field(init=False)
    ended: tuple[tuple[str, str, str], ...] = field(init=False)
    in_flight: Mapping[str, tuple[str, ...]] = field(init=False)
    reconciled: tuple[tuple[str, str], ...] = field(init=False)
    effects_unknown: tuple[tuple[str, str], ...] = field(init=False)

    def __post_init__(self) -> None:
        """Flatten the per-owner reports."""
        o = self.owners
        closed = tuple((r.owner, s) for r in o for s in r.closed_unknown)
        ended = tuple((r.owner, run, why) for r in o for run, why in r.ended)
        waiting = MappingProxyType({r.owner: r.in_flight for r in o if r.in_flight})
        object.__setattr__(self, "closed_unknown", closed)
        object.__setattr__(self, "ended", ended)
        object.__setattr__(self, "in_flight", waiting)
        object.__setattr__(self, "reconciled", tuple((r.owner, k) for r in o for k in r.reconciled))
        unknown = tuple((r.owner, s) for r in o for s in r.effects_unknown)
        object.__setattr__(self, "effects_unknown", unknown)


def recover_owner(
    store: EventStore,
    owner: str,
    loaded: Mapping[str, str],
    *,
    outbox: OutboxWorker | None = None,
    source_check: SourceCheck | None = None,
) -> OwnerRecovery:
    """Steps 1 to 6 of the module docstring for one owner. ``loaded``: agent to hash.

    ``outbox`` is the owner's outbox worker; it may be None only when the owner has no
    outbox entry left to finish. ``source_check`` is step 2.
    """
    if not store.is_writer:
        raise NotRecoveryWriter(message="recovery needs the writer's store handle")
    reconciled, moved = _reconcile_outbox(store, owner, outbox, source_check)
    view = load_view(store, owner)
    runs = rebuild(RUNS, view)
    still_open = open_runs(runs)
    closed: list[str] = []
    for step in dangling_steps(view):
        if step.run in still_open and step.semantics == "at_most_once" and step.dispatched:
            _append_once(store, owner, facts.STEP_RESULT, facts.unknown_completion(step))
            closed.append(step.step_id)
    ended: list[tuple[str, str]] = []
    for run in still_open:  # a person's stop first: its ending may hold the agent's other runs
        if pending_stop(view, run) is not None:
            _append_once(store, owner, STOP_DECISION, *stop_ending(view, run, RESUME_CANDIDATE))
            ended.append((run, STOP_REQUESTED))
    if ended:
        view = load_view(store, owner)
    suspended: list[str] = []
    in_flight: list[str] = []
    held: list[str] = []
    for run in still_open:
        if (run, STOP_REQUESTED) in ended:
            continue
        summary = runs[run]
        cls = _classify(view, summary, loaded.get(summary.agent))
        if cls == "definition_changed":
            _append_once(store, owner, STOP_DECISION, *changed_ending(view, run))
            ended.append((run, DEFINITION_CHANGED))
        elif cls == "suspended":
            suspended.append(run)
        elif cls == "held":
            held.append(run)
        else:
            in_flight.append(run)
    unknown = effects_unknown_steps(view, open_runs(rebuild(RUNS, load_view(store, owner))))
    return OwnerRecovery(
        owner, tuple(closed), tuple(ended), tuple(suspended), tuple(in_flight), tuple(held),
        reconciled, unknown, moved,
    )  # fmt: skip


def _reconcile_outbox(
    store: EventStore, owner: str, outbox: OutboxWorker | None, source_check: SourceCheck | None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Steps 1 to 3: the landed phase, the source check (ADR-P05 S2), the unlanded phase."""
    if not pending_outbox(load_view(store, owner)):
        return (), () if source_check is None else source_check(store, owner)
    if outbox is None:
        raise EffectNeedsOutbox(
            message=f"{owner} has outbox entries to finish and recovery was given no worker"
        )
    if outbox.owner != owner:
        raise ValueError(f"the outbox worker runs {outbox.owner}'s entries, not {owner}'s")
    landed = outbox.reconcile("landed")
    moved = () if source_check is None else source_check(store, owner)
    unlanded = outbox.reconcile("unlanded")
    return tuple(e.key for e in (*landed, *unlanded)), moved


OutboxFor = Callable[[str], OutboxWorker]
"""Builds an owner's outbox worker, for recovery across owners."""


def recover_on_startup(
    store: EventStore,
    loaded: Mapping[str, str],
    *,
    outbox: OutboxFor | None = None,
    owners: Iterable[str] = (),
    source_check: SourceCheck | None = None,
) -> RecoveryReport:
    """Recover every owner with an open run, and ``owners``; drive nothing (D10).

    ``loaded`` maps an agent to its definition hash; ``outbox`` builds each owner's outbox
    worker (needed for any owner with entries to finish); ``source_check`` is step 2.
    """
    if not store.is_writer:
        raise NotRecoveryWriter(message="recovery needs the writer's store handle")
    every = dict.fromkeys((*store.owners_with_open_runs(), *owners))
    return RecoveryReport(
        tuple(
            recover_owner(
                store,
                o,
                loaded,
                outbox=None if outbox is None else outbox(o),
                source_check=source_check,
            )
            for o in every
        )
    )


def resume_owner(
    store: EventStore,
    owner: str,
    driver: RunDriver,
    *,
    clock: Clock,
    loaded: Mapping[str, str],
    limits: Limits = DEFAULT_LIMITS,
    outbox: OutboxWorker | None = None,
) -> tuple[str, ...]:
    """Recover ``owner`` again, then resume each of its in-flight runs; return the runs driven.

    Called before the owner's next mutating command and by ``aksha resume``. A run the
    recovery ends or holds is never driven; a run past its restart limit is ended
    ``failed, restart_limit`` by its resume record and not driven either. ``outbox`` is
    the owner's outbox worker, for recovery and for the resumed runs' effectful steps.
    """
    report = recover_owner(store, owner, loaded, outbox=outbox)
    driven: list[str] = []
    for run in report.in_flight:
        if not _resume_record(store, owner, run, limits):
            continue
        runs = rebuild(RUNS, load_view(store, owner))
        steps = StepExecutor(store, owner, clock, outbox=outbox)
        driver(runs[run], steps)
        steps.drain_late()
        honour_stop(store, owner, run, TURN_END)
        driven.append(run)
    return tuple(driven)


RESTART_LIMIT = "restart_limit"
"""The reason of a run the restart limit ended (ADR-P02 D8)."""


def _resume_record(store: EventStore, owner: str, run: str, limits: Limits) -> bool:
    """Write the resume record of ``run``; True when it may be driven, False otherwise.

    A run a person's stop holds gets no record and is not driven (ADR-P16 D6).
    """
    view = load_view(store, owner)
    summary = rebuild(RUNS, view)[run]
    if stop_hold(view, run=run, agent=summary.agent) is not None:
        return False
    walk = 1 + sum(1 for e in view.events if e.run == run and e.type == "stop.decided")
    resumes = resumes_since_progress(view, run)
    if resumes >= limits.restart_limit:
        counts = {"resumes": resumes, "restart_limit": limits.restart_limit}
        record = facts.resume_record(run, walk, verdict="stop", reason=RESTART_LIMIT,
                                     limit=RESTART_LIMIT, counts=counts)  # fmt: skip
        ended = NewEvent("run.ended", {"outcome": "failed", "reason": RESTART_LIMIT}, run=run)
        store.append(owner, STOP_DECISION, (ended, record))
        return False
    store.append(owner, STOP_DECISION, (facts.resume_record(run, walk, verdict="continue"),))
    return True


def _append_once(store: EventStore, owner: str, boundary: str, *events: NewEvent) -> None:
    """Append ``events`` in one transaction; a dedup-key conflict is not an error."""
    with contextlib.suppress(AlreadyRecorded):
        store.append(owner, boundary, events)
