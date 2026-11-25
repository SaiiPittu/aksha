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

``next_instances`` returns the first ``tree.concurrency`` of them; v1 compiles only 1, so
two fake-model runs start instances in the same order. ``schedule`` returns everything a
stop decision commits for the scheduler (U3): ``task.withdrawn`` and
``interrupt.voided{reason: key_withdrawn}`` for keys that left their source, and
``task.entered{cause: scheduler}`` with the entry's ``completion_check.evaluated`` for each
new pick. Only those facts start work: a model or a stop policy that asks for work gets
nothing; a model proposes work only by saving data a key source reads.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from aksha.runtime.artifacts.types import ArtifactRef, WriterId
from aksha.runtime.artifacts.versions import unaccepted_revisions
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.completion import (
    evaluate_in,
    evaluated_fact,
    reset_fact,
    task_done,
)
from aksha.runtime.tasks.errors import TaskUnknown
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

    def walk(self, paths: tuple[str, ...], key: str | None, keyed: bool) -> Iterator[InstanceRef]:
        for path in paths:
            refs = (InstanceRef(self.state.run, path, key),) if keyed else self.state.refs(path)
            for ref in refs:
                yield from self.consider(ref)

    def consider(self, ref: InstanceRef) -> Iterator[InstanceRef]:
        state = self.state
        status = state.status(ref)
        if status in SETTLED or status == "suspended":
            return
        if not self.needs_met(ref) or self.awaiting_review(ref):
            return
        node = state.tree.node(ref.task_path)
        if node.is_leaf or status == "new":
            yield ref
            return
        below = tuple(self.walk(node.children, ref.key, True))
        if is_supervisor(node.task):
            move = supervisor_step(state, ref, below)
            if move.move == "push":
                yield from below
            elif move.move != "hold":
                yield ref
            return
        if below:
            yield from below
        elif all(state.settled(ref.child(c.id)) for c in node.task.children):
            yield ref

    def needs_met(self, ref: InstanceRef) -> bool:
        state = self.state
        node = state.tree.node(ref.task_path)
        prefix = "" if node.parent is None else f"{node.parent}/"
        mine = state.tree.key_source(ref.task_path)
        for need in node.task.needs:
            path = prefix + need
            theirs = state.tree.key_source(path)
            if ref.key is not None and _same_source(mine, theirs):
                if state.status(InstanceRef(ref.run, path, ref.key)) != "done":
                    return False
            elif not task_done(state, path).ok:
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
    return a is not None and b is not None and a == b


def runnable_in(state: RunState) -> tuple[InstanceRef, ...]:
    """:func:`runnable` over an indexed run state."""
    return tuple(_Pick(state).walk(state.tree.roots, None, False))


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


def entered_fact(tree: CompiledTree, ref: InstanceRef, *, cause: EntryCause) -> NewEvent:
    """The ``task.entered`` fact for ``ref`` (U3), naming its key source and writer key."""
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
    """``task.entered`` and the entry's ``completion_check.evaluated`` for ``ref``. Pure."""
    evaluation = evaluate_in(run_state(view, tree, ref.run), ref)
    facts = (entered_fact(tree, ref, cause=cause), evaluated_fact(ref, evaluation, on="entry"))
    return Entry(ref, facts, evaluation.ok)


def withdrawals(view: LogView, tree: CompiledTree, run: str) -> tuple[NewEvent, ...]:
    """``task.withdrawn`` and ``interrupt.voided{key_withdrawn}`` for keys that left (D3).

    An instance is withdrawn once: an instance whose last fact is already a withdrawal
    gets nothing new. Its open interrupts are voided, so a withdrawn key asks no one.
    """
    state = run_state(view, tree, run)
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
    done: list[InstanceRef] = []
    for ref in picks:
        if state.status(ref) == "new":
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
) -> tuple[NewEvent, ...]:
    """Re-enter the instance a writer key names (a rebuild run, slice 6), whatever its run.

    A blocked instance is reset first (``completion_check.reset{cause: rebuild}``), so its
    veto count starts again. ``task_unknown`` for a writer of another agent or Task.
    """
    if writer.agent != tree.agent:
        raise TaskUnknown(message=f"writer {writer.agent!r} is not agent {tree.agent!r}")
    ref = InstanceRef(run, writer.task_path, writer.key)
    tree.node(ref.task_path)
    facts: list[NewEvent] = []
    if run_state(view, tree, run).status(ref) == "blocked":
        facts.append(reset_fact(ref, "rebuild"))
    facts.append(entered_fact(tree, ref, cause=cause))
    return tuple(facts)


Popped = Literal["done", "fail"]


def pop_fields(popped: Popped) -> dict[str, str]:
    """The fields a ``stop.decided`` record carries when it pops its instance (D6)."""
    return {"pop": popped}


@dataclass(frozen=True)
class RunOutcome:
    """How the Task tree ended: ``complete``, or ``partial`` naming the blocked instances."""

    outcome: Literal["complete", "partial"]
    blockers: tuple[str, ...] = ()


def run_outcome(view: LogView, tree: CompiledTree, run: str) -> RunOutcome | None:
    """None while an instance may run or is suspended; else the tree's outcome. Pure."""
    state = run_state(view, tree, run)
    if runnable_in(state):
        return None
    refs = tuple(all_instances(state))
    if any(state.status(r) == "suspended" for r in refs):
        return None
    blocked = tuple(r.instance for r in refs if state.status(r) == "blocked")
    if not blocked and all(task_done(state, root).ok for root in tree.roots):
        return RunOutcome("complete")
    return RunOutcome("partial", blocked)
