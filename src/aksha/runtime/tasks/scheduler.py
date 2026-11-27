"""The scheduler (ADR-P15 D5): the one component that starts Task instances.

``runnable`` is a pure function of the compiled tree, the key sources and the derived
statuses. Its pick order: declared order, then depth first per key, then keys sorted as
strings. An instance is skipped when it is done, blocked or withdrawn; when it is
suspended on an open interrupt; when a sibling it ``needs`` is not done (for the same key
when both fan out over one key source, else over every key); and when a version of a
kind it reads is a pending revision awaiting a person's review (ADR-P05 D1; F2: never
its own writes). For a parent instance:

1. ``new``: the parent itself is picked, so its entry is recorded (and its check run);
2. a supervisor (``supervisor.is_supervisor``) follows the supervisor rule;
3. otherwise its runnable children are picked in its place, and the parent itself once
   every child instance is settled, so the stop decision evaluates and pops it.

Invalidation (ADR-P05 D7, D8, F2): the rebuild plan, recomputed from the log at every
pick, re-enters instances by writer key (:func:`rebuild_targets`). An open run picks
its own settled work again when the plan lists it (in place); a rebuild run picks only
what the plan re-enters, never its tree from the roots, and orders it by the plan's
holds rather than ``needs``. A re-entry commits ``task.entered{cause: rebuild}`` (with a
reset for a blocked instance), never an entry check, so the old version cannot pop it.

``next_instances`` returns the first ``tree.concurrency`` of them; v1 compiles only 1, so
two fake-model runs start instances in the same order. ``schedule`` returns everything a
stop decision commits for the scheduler (U3): ``task.withdrawn`` and
``interrupt.voided{reason: key_withdrawn}`` for keys that left their source, and
``task.entered{cause: scheduler}`` with the entry's ``completion_check.evaluated`` for each
new pick. Only those facts start work: a model or a stop policy that asks for work gets
nothing; a model proposes work only by saving data a key source reads.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from aksha.runtime.artifacts.types import ArtifactRef, WriterId
from aksha.runtime.artifacts.versions import unaccepted_revisions
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.finalization.types import UnknownEntry
from aksha.runtime.interrupts.reducers import APPROVAL_VOIDED, approvals
from aksha.runtime.invalidation.confirm import after
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.completion import (
    evaluate_in,
    evaluated_fact,
    reset_fact,
    task_done,
)
from aksha.runtime.tasks.errors import TaskUnknown
from aksha.runtime.tasks.rebuilds import is_rebuild_run, rebuild_targets
from aksha.runtime.tasks.reuse import reuse_facts
from aksha.runtime.tasks.status import (
    TASK_ENTERED,
    TASK_WITHDRAWN,
    RunState,
    all_instances,
    run_state,
)
from aksha.runtime.tasks.supervisor import is_supervisor, supervisor_step
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import SETTLED, EntryCause, InstanceRef, KeySource

INTERRUPT_VOIDED = "interrupt.voided"
KEY_WITHDRAWN = "key_withdrawn"


class _Pick:
    """One pick over an indexed run state; holds the pending revisions read once."""

    def __init__(self, state: RunState) -> None:
        self.state = state
        refs = []
        for text in unaccepted_revisions(state.view):
            try:
                refs.append(ArtifactRef.parse(text))
            except ValueError:
                continue
        self.pending = tuple(refs)
        self.rebuild_run = is_rebuild_run(state.view, state.run)
        self.targets = rebuild_targets(state.view, state.tree, state.run)

    def reentries(self) -> Iterator[InstanceRef]:
        """The targets to re-enter: new in a rebuild run, or settled (done, blocked) here."""
        for ref in self.targets:
            if self.state.status(ref) in ("new", "done", "blocked"):
                yield ref

    def rebuild_walk(self) -> Iterator[InstanceRef]:
        """A rebuild run's picks: what it already re-entered, then the targets (D8).

        A rebuild run never walks its tree from the roots: it runs only the instances the
        plan re-enters by writer key, so work the change did not touch stays untouched.
        """
        state = self.state
        seen: set[InstanceRef] = set()
        for instance, mark in state.last.items():
            ref = InstanceRef.parse(state.run, instance)
            if mark in ("entered", "reset") and ref not in seen:
                seen.add(ref)
                yield from self.consider(ref)
        for ref in self.reentries():
            if ref not in seen:
                seen.add(ref)
                yield ref

    def walk(self, refs: tuple[InstanceRef, ...]) -> Iterator[InstanceRef]:
        for ref in refs:
            yield from self.consider(ref)

    def roots(self) -> Iterator[InstanceRef]:
        for path in self.state.tree.roots:
            yield from self.walk(self.state.refs(path))

    def consider(self, ref: InstanceRef) -> Iterator[InstanceRef]:
        state = self.state
        status = state.status(ref)
        if status in SETTLED or status == "suspended" or state.voided(ref):
            return
        if self.awaiting_review(ref):
            return
        if not self.rebuild_run and not self.needs_met(ref):
            return  # a rebuild run's order is the plan's (held_by), not the tree's needs
        node = state.tree.node(ref.task_path)
        if node.is_leaf or status == "new":
            yield ref
            return
        below = tuple(self.walk(state.children(ref)))
        if is_supervisor(node.task):
            move = supervisor_step(state, ref, below)
            if move.move == "push":
                yield from below
            elif move.move != "hold":
                yield ref
            return
        if below:
            yield from below
        elif all(state.settled(c) for c in state.children(ref)):
            yield ref

    def needs_met(self, ref: InstanceRef) -> bool:
        state = self.state
        node = state.tree.node(ref.task_path)
        prefix = "" if node.parent is None else f"{node.parent}/"
        mine = state.tree.key_source(ref.task_path)
        finalizing = state.finalizing is not None
        for need in node.task.needs:
            path = prefix + need
            theirs = state.tree.key_source(path)
            if ref.key is not None and _same_source(mine, theirs):
                other = InstanceRef(ref.run, path, ref.key)
                if finalizing and state.settled(other):
                    continue  # ADR-P24 D4: a finalizer reads what settled, unknowns and all
                if state.status(other) != "done":
                    return False
                continue
            found = task_done(state, path)
            if finalizing and found.code in ("child_blocked", "keys_pending"):
                continue  # D4: nothing in it may run again, so the finalizer goes ahead
            if not found.ok:
                return False
        return True

    def awaiting_review(self, ref: InstanceRef) -> bool:
        if not self.pending:
            return False
        tree = self.state.tree
        writes = {tree.kind(w) for w in tree.node(ref.task_path).task.writes}
        for read in tree.reads(ref.task_path):
            if read.kind in writes:
                continue  # F2: a writer is never held by its own pending review
            for rev in self.pending:
                if rev.kind == read.kind and (
                    read.keys != "own" or rev.key is None or rev.key == ref.key
                ):
                    return True
        return False


def _same_source(a: KeySource | None, b: KeySource | None) -> bool:
    """Whether two key sources key the same items, so a ``needs`` binds key to key.

    A key filter (``unless``) only narrows the keys one Task takes, so a filtered source
    still keys the same items as its unfiltered sibling (a supervisor's deep tier needs
    the same item's triage, not every item's).
    """
    if a is None or b is None:
        return False
    return (a.origin, a.name, a.path) == (b.origin, b.name, b.path)


def runnable_below(state: RunState, ref: InstanceRef) -> tuple[InstanceRef, ...]:
    """The runnable instances under parent ``ref``, in pick order (what a supervisor reads)."""
    return tuple(_Pick(state).walk(state.children(ref)))


def runnable_in(state: RunState) -> tuple[InstanceRef, ...]:
    """:func:`runnable` over an indexed run state.

    A rebuild run picks only what the plan re-enters (``_Pick.rebuild_walk``). Any other
    run picks the plan's re-entries of its own settled work first (D7: an open run
    regenerates its own work in place), then its tree in pick order.
    """
    pick = _Pick(state)
    if pick.rebuild_run:
        return tuple(dict.fromkeys(pick.rebuild_walk()))
    walked = pick.roots()
    return tuple(dict.fromkeys((*pick.reentries(), *walked)))


def runnable(view: LogView, tree: CompiledTree, run: str) -> tuple[InstanceRef, ...]:
    """Every instance that may start or continue now, in pick order (module doc). Pure."""
    return runnable_in(run_state(view, tree, run))


def next_instances(view: LogView, tree: CompiledTree, run: str) -> tuple[InstanceRef, ...]:
    """The first ``tree.concurrency`` runnable instances (one in v1)."""
    return runnable(view, tree, run)[: tree.concurrency]


def next_instance(view: LogView, tree: CompiledTree, run: str) -> InstanceRef | None:
    """``runnable(...)[0]``, or None when nothing may run now."""
    found = runnable(view, tree, run)
    return found[0] if found else None


def writer_of(tree: CompiledTree, ref: InstanceRef) -> WriterId:
    """The writer key of ``ref``: (agent, Task path, key), the same in every run."""
    return WriterId(tree.agent, ref.task_path, ref.key)


def entered_fact(
    tree: CompiledTree,
    ref: InstanceRef,
    *,
    cause: EntryCause,
    reentry: Mapping[str, Any] | None = None,
) -> NewEvent:
    """The ``task.entered`` fact for ``ref`` (U3), naming its key source and writer key.

    ``reentry`` is a re-entry's recorded mode choice (decision 36,
    ``context.reentry.ReentryChoice.as_json``).
    """
    tree.node(ref.task_path)
    payload: dict[str, object] = {
        "cause": cause,
        "task": ref.task_path,
        "key": ref.key,
        "writer": writer_of(tree, ref).as_json(),
    }
    source = tree.key_source(ref.task_path)
    if source is not None:
        named: dict[str, str] = {"origin": source.origin, "name": source.name}
        if source.path is not None:
            named["path"] = source.path
        payload["key_source"] = named
    if reentry is not None:
        payload["reentry"] = dict(reentry)
    return NewEvent(TASK_ENTERED, payload, run=ref.run, instance=ref.instance)


@dataclass(frozen=True)
class Entry:
    """An instance's entry: the facts to commit, and whether its check passed on entry.

    ``done`` True means the stop decision pops it DONE with no model turn, which is how
    work a change did not touch stays untouched.
    """

    ref: InstanceRef
    facts: tuple[NewEvent, ...]
    done: bool


def enter(view: LogView, tree: CompiledTree, ref: InstanceRef, *, cause: EntryCause) -> Entry:
    """``task.entered`` and the entry's ``completion_check.evaluated`` for ``ref``. Pure.

    Between them, the ``artifact.reused`` facts of an earlier run's versions this entry
    reuses (ADR-P04 R1, ``tasks.reuse``); the entry's check reads the log with them.
    """
    reused = reuse_facts(view, ref.run, writer_of(tree, ref), ref.instance)
    seen = after(view, reused) if reused else view
    evaluation = evaluate_in(run_state(seen, tree, ref.run), ref)
    facts = (entered_fact(tree, ref, cause=cause), *reused,
             evaluated_fact(ref, evaluation, on="entry"))  # fmt: skip
    return Entry(ref, facts, evaluation.ok)


def withdrawals(view: LogView, tree: CompiledTree, run: str) -> tuple[NewEvent, ...]:
    """``task.withdrawn`` and ``interrupt.voided{key_withdrawn}`` for keys that left (D3).

    An instance is withdrawn once: an instance whose last fact is already a withdrawal
    gets nothing new. Its open interrupts are voided, so a withdrawn key asks no one, and
    its live approvals (requested or granted) are voided with them (ADR-P17 S3).
    """
    state = run_state(view, tree, run)
    live = [a for a in approvals(view).values()
            if a.run == run and a.state in ("requested", "granted")]  # fmt: skip
    out: list[NewEvent] = []
    for ref in all_instances(state):
        mark = state.last.get(ref.instance)
        if mark is None or mark == "withdrawn" or state.status(ref) != "withdrawn":
            continue
        payload = {"task": ref.task_path, "key": ref.key, "reason": KEY_WITHDRAWN}
        out.append(NewEvent(TASK_WITHDRAWN, payload, run=run, instance=ref.instance))
        for ident in state.open_interrupts.get(ref.instance, ()):
            voided = {"interrupt": ident, "reason": KEY_WITHDRAWN}
            out.append(NewEvent(INTERRUPT_VOIDED, voided, run=run, instance=ref.instance))
        for a in live:
            if a.instance == ref.instance:
                payload = {"approval": a.approval_id, "reason": KEY_WITHDRAWN}
                out.append(NewEvent(APPROVAL_VOIDED, payload, run=run, instance=ref.instance))
    return tuple(out)


@dataclass(frozen=True)
class Schedule:
    """What one stop decision commits for the scheduler, and what it picked.

    ``facts`` go into the stop decision's transaction (U3) before its ``stop.decided``;
    ``picks`` are the instances to push or continue; ``entered_done`` are the new picks
    whose check passed on entry (popped DONE with no turn).
    """

    facts: tuple[NewEvent, ...]
    picks: tuple[InstanceRef, ...]
    entered_done: tuple[InstanceRef, ...]


def schedule(view: LogView, tree: CompiledTree, run: str) -> Schedule:
    """The scheduler's part of one stop decision (module doc). Pure: it never appends."""
    facts = list(withdrawals(view, tree, run))
    state = run_state(view, tree, run)
    picks = runnable_in(state)[: tree.concurrency]
    targets = rebuild_targets(view, tree, run)
    done: list[InstanceRef] = []
    for ref in picks:
        if ref in targets and state.status(ref) in ("new", "done", "blocked"):
            facts.extend(reentry(view, tree, run, writer_of(tree, ref)))
        elif state.status(ref) == "new":
            entry = enter(view, tree, ref, cause="scheduler")
            facts.extend(entry.facts)
            if entry.done:
                done.append(ref)
    return Schedule(tuple(facts), picks, tuple(done))


def reentry(
    view: LogView,
    tree: CompiledTree,
    run: str,
    writer: WriterId,
    *,
    cause: EntryCause = "rebuild",
    choice: Mapping[str, Any] | None = None,
) -> tuple[NewEvent, ...]:
    """Re-enter the instance a writer key names (a rebuild run, slice 6), whatever its run.

    A blocked instance is reset first (``completion_check.reset{cause: rebuild}``), so its
    veto count starts again. ``choice`` is the re-entry's mode choice, recorded on the
    ``task.entered`` (decision 36). ``task_unknown`` for a writer of another agent or Task.
    """
    if writer.agent != tree.agent:
        raise TaskUnknown(message=f"writer {writer.agent!r} is not agent {tree.agent!r}")
    tree.node(writer.task_path)
    ref = tree.ref(run, writer.task_path, writer.key)
    facts: list[NewEvent] = []
    if run_state(view, tree, run).status(ref) == "blocked":
        facts.append(reset_fact(ref, "rebuild"))
    facts.append(entered_fact(tree, ref, cause=cause, reentry=choice))
    return tuple(facts)


Popped = Literal["done", "fail"]


def pop_fields(popped: Popped) -> dict[str, str]:
    """The fields a ``stop.decided`` record carries when it pops its instance (D6)."""
    return {"pop": popped}


@dataclass(frozen=True)
class RunOutcome:
    """How the Task tree ended (ADR-P24 D6).

    ``complete``: every top-level Task is done and no output holds an unknown.
    ``partial``: every top-level Task is done with at least one explicit unknown, or the
    run was finalized. ``blocked``: a block nothing tolerates left work undone.
    ``blockers`` names the blocked instances; ``unknowns`` every explicit unknown.
    """

    outcome: Literal["complete", "partial", "blocked"]
    blockers: tuple[str, ...] = ()
    unknowns: tuple[UnknownEntry, ...] = ()


def run_outcome(view: LogView, tree: CompiledTree, run: str) -> RunOutcome | None:
    """None while an instance may run or is suspended; else the tree's outcome. Pure.

    A rebuild run is ``complete`` once nothing it re-enters may run, and ``blocked`` if
    a re-entered instance blocked; the rest of its tree was never meant to run (D8).
    """
    from aksha.runtime.finalization.unknowns import unknowns_in  # deferred: it reads tasks

    state = run_state(view, tree, run)
    if runnable_in(state):
        return None
    refs = tuple(all_instances(state))
    if any(state.status(r) == "suspended" and not state.voided(r) for r in refs):
        return None
    blocked = tuple(r.instance for r in refs if state.status(r) == "blocked")
    if is_rebuild_run(view, run):  # D8: its outcome is the re-entered work's alone
        return RunOutcome("blocked" if blocked else "complete", blocked)
    found = unknowns_in(state)
    if state.finalizing is not None:
        return RunOutcome("partial", blocked, found)
    if all(task_done(state, root).ok for root in tree.roots):
        return RunOutcome("partial" if found else "complete", blocked, found)
    return RunOutcome("blocked", blocked, found)
