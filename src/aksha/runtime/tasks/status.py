"""Instance status as a projection (ADR-P15 D6): computed from facts, never stored.

The facts, each written by another part of the runtime:

======================================  ==================================================
Fact (envelope ``run`` and ``instance``)  Moves the instance to
======================================  ==================================================
``task.entered``                        ``running`` (entry or re-entry by writer key)
``stop.decided{pop: done}``             ``done`` (the stop decision popped it DONE)
``stop.decided{pop: fail}``             ``blocked`` (stop-veto limit, ``check_raised``)
``completion_check.reset``              ``running`` again after a block
``task.withdrawn``                      ``withdrawn``; ``new`` once its key returns (S3)
``interrupt.raised`` (open)             ``suspended`` while the instance is running
======================================  ==================================================

An instance whose key is not in its key source now is ``withdrawn`` whatever its facts
say; one with no fact is ``new``. The key sources are read through ``keys.keys_of`` and
filtered by the source's ``unless`` check. Because every arrow is a recorded fact, the
status after a restart is the status before it (``test_status_equals_full_rebuild``).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from aksha.runtime.interrupts.reducers import CLOSING, INTERRUPT_RAISED
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.checks import run_check
from aksha.runtime.tasks.keys import keys_of
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import SETTLED, InstanceRef, InstanceStatus

TASK_ENTERED = "task.entered"
TASK_WITHDRAWN = "task.withdrawn"
CHECK_EVALUATED = "completion_check.evaluated"
CHECK_RESET = "completion_check.reset"
STOP_DECIDED = "stop.decided"

LIFECYCLE = frozenset({TASK_ENTERED, TASK_WITHDRAWN, CHECK_RESET})
"""The Task facts that move a status; with ``stop.decided`` carrying ``pop``."""


def lifecycle_mark(event_type: str, pop: object) -> str | None:
    """What one fact means for its instance: entered, done, fail, reset, withdrawn or None."""
    if event_type == TASK_ENTERED:
        return "entered"
    if event_type == TASK_WITHDRAWN:
        return "withdrawn"
    if event_type == CHECK_RESET:
        return "reset"
    if event_type == STOP_DECIDED and pop in ("done", "fail"):
        return str(pop)
    return None


@dataclass(frozen=True)
class RunState:
    """One run's Task facts, indexed once: the last lifecycle mark and open interrupts.

    Built by :func:`run_state`; every status and pick reads it, so one decision reads the
    event store's facts once.
    """

    view: LogView
    tree: CompiledTree
    run: str
    last: Mapping[str, str]
    last_seq: Mapping[str, int]
    open_interrupts: Mapping[str, tuple[str, ...]]
    seen_keys: Mapping[str, tuple[str, ...]]
    _keys: dict[str, tuple[str | None, ...] | None] = field(default_factory=dict, repr=False)

    def keys(self, path: str) -> tuple[str | None, ...] | None:
        """The keys the Task at ``path`` has now: ``(None,)`` when un-keyed (module doc)."""
        top = self.tree.node(path).top
        if top not in self._keys:
            self._keys[top] = instance_keys(self.view, self.tree, self.run, top)
        return self._keys[top]

    def refs(self, path: str) -> tuple[InstanceRef, ...]:
        """Every instance of ``path``: current keys and keys with facts, sorted as strings."""
        now = self.keys(path)
        if now == (None,):
            return (InstanceRef(self.run, path, None),)
        found = {k for k in now or () if k is not None}
        found |= set(self.seen_keys.get(self.tree.node(path).top, ()))
        return tuple(InstanceRef(self.run, path, k) for k in sorted(found))

    def status(self, ref: InstanceRef) -> InstanceStatus:
        """The instance's status (module doc)."""
        now = self.keys(ref.task_path)
        if now is None or ref.key not in now:
            return "withdrawn"
        mark = self.last.get(ref.instance)
        if mark is None or mark == "withdrawn":
            return "new"
        if mark == "done":
            return "done"
        if mark == "fail":
            return "blocked"
        return "suspended" if self.open_interrupts.get(ref.instance) else "running"

    def settled(self, ref: InstanceRef) -> bool:
        """True when the instance is done, blocked or withdrawn."""
        return self.status(ref) in SETTLED


def run_state(view: LogView, tree: CompiledTree, run: str) -> RunState:
    """Index ``run``'s Task facts in ``view`` (one pass over the events). Pure."""
    last: dict[str, str] = {}
    last_seq: dict[str, int] = {}
    raised: dict[str, str] = {}
    closed: set[str] = set()
    seen: dict[str, set[str]] = {}
    for e in view.events:
        if e.run != run:
            continue
        if e.type in CLOSING:  # a person's answer names the interrupt, not the instance
            closed.add(str(e.payload.get("interrupt", e.step or "")))
            continue
        if e.instance is None:
            continue
        if e.type == INTERRUPT_RAISED:
            raised[str(e.payload.get("interrupt", e.step or ""))] = e.instance
            continue
        mark = lifecycle_mark(e.type, e.payload.get("pop"))
        if mark is None:
            continue
        last[e.instance] = mark
        last_seq[e.instance] = e.seq
        ref = InstanceRef.parse(run, e.instance)
        if ref.key is not None:
            seen.setdefault(ref.task_path.split("/", 1)[0], set()).add(ref.key)
    open_by: dict[str, list[str]] = {}
    for ident, instance in raised.items():
        if ident not in closed:
            open_by.setdefault(instance, []).append(ident)
    return RunState(
        view,
        tree,
        run,
        MappingProxyType(last),
        MappingProxyType(last_seq),
        MappingProxyType({k: tuple(v) for k, v in open_by.items()}),
        MappingProxyType({k: tuple(sorted(v)) for k, v in seen.items()}),
    )


def instance_keys(
    view: LogView, tree: CompiledTree, run: str, path: str
) -> tuple[str | None, ...] | None:
    """The keys of the Task at ``path``: ``(None,)`` un-keyed, None while its source is empty.

    A key for which the source's ``unless`` check passes is left out (v2 LA-4). The check
    is run like a completion check (a raise is a FAIL, so the key stays).
    """
    from aksha.runtime.tasks.view import TaskView  # deferred: the view reads statuses

    node = tree.node(tree.node(path).top)
    source = node.task.per
    if source is None:
        return (None,)
    kind = tree.kind(source.name) if source.origin == "artifact" else None
    keys = keys_of(view, source, kind=kind)
    if keys is None:
        return None
    if source.unless is None:
        return keys
    unless = source.unless
    kept = []
    for key in keys:
        result, _ = run_check(unless, TaskView(view, tree, InstanceRef(run, node.path, key)))
        if not result.ok:
            kept.append(key)
    return tuple(kept)


def instance_status(view: LogView, tree: CompiledTree, ref: InstanceRef) -> InstanceStatus:
    """The status of one instance (a reducer over ``view``, D6)."""
    return run_state(view, tree, ref.run).status(ref)


def all_instances(state: RunState) -> Iterator[InstanceRef]:
    """Every instance of the run, in pick order: declared order, depth first per key."""

    def under(paths: tuple[str, ...], key: str | None, keyed_parent: bool) -> Iterator[InstanceRef]:
        for path in paths:
            refs = (InstanceRef(state.run, path, key),) if keyed_parent else state.refs(path)
            for ref in refs:
                yield ref
                yield from under(state.tree.node(path).children, ref.key, True)

    yield from under(state.tree.roots, None, False)


def statuses(view: LogView, tree: CompiledTree, run: str) -> Mapping[str, InstanceStatus]:
    """Every instance's status, by instance ID, in pick order."""
    state = run_state(view, tree, run)
    return MappingProxyType({r.instance: state.status(r) for r in all_instances(state)})


@dataclass(frozen=True)
class TaskTreeRow:
    """One row of the session API's Task tree view: an instance and its status."""

    instance: str
    task_path: str
    key: str | None
    depth: int
    status: InstanceStatus


def task_tree(view: LogView, tree: CompiledTree, run: str) -> tuple[TaskTreeRow, ...]:
    """The run's instances with their statuses, for a live panel (session API read view)."""
    state = run_state(view, tree, run)
    return tuple(
        TaskTreeRow(r.instance, r.task_path, r.key, tree.node(r.task_path).depth, state.status(r))
        for r in all_instances(state)
    )


def withdrawn_writers(view: LogView, tree: CompiledTree, run: str) -> tuple[InstanceRef, ...]:
    """The instances of ``run`` that are withdrawn now (S3: their versions read ``gone``)."""
    state = run_state(view, tree, run)
    return tuple(r for r in all_instances(state) if state.status(r) == "withdrawn")
