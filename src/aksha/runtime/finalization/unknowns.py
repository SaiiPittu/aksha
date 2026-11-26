"""Explicit unknowns as a projection (ADR-P24 D2, D4, D6): never written as artifacts.

An instance's output is an explicit unknown when:

1. ``unverified`` (D2): it is blocked, or unreached (an instance it needs for its key is
   blocked), and the Task that holds it tolerates that: its parent, or its keyed Task
   taken as a whole, declares ``on_child_blocked="partial"``. A block nothing tolerates
   is not an unknown; it leaves the run ``blocked``.
2. ``not_checked`` (D4): finalization voided it. Only the topmost voided instance is
   listed; its subtree's outputs are unknown with it.

``entries`` is what a reader sees for a kind: the current version of each key, and an
``Unknown`` for each key whose writer is listed above, so the value comes from the read
view and can never disagree with the event store. ``unknown_reads`` is the same for the
reads a Task declares, for its context (ADR-P08 D4).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from aksha.runtime.artifacts.types import ArtifactVersion
from aksha.runtime.finalization.types import UnknownEntry
from aksha.runtime.grounding.types import Unknown
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.status import RunState, all_instances, run_state
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import SETTLED, InstanceRef

RESERVE_SPENT = "reserve_spent"
"""The blocker of a finalizer the reserve ran out under (D5)."""


def _parent(state: RunState, ref: InstanceRef) -> InstanceRef | None:
    node = state.tree.node(ref.task_path)
    return None if node.parent is None else InstanceRef(ref.run, node.parent, ref.key)


def _under_voided(state: RunState, ref: InstanceRef) -> bool:
    parent = _parent(state, ref)
    while parent is not None:
        if state.voided(parent):
            return True
        parent = _parent(state, parent)
    return False


def tolerated(state: RunState, ref: InstanceRef) -> bool:
    """Whether the Task holding ``ref`` declares ``on_child_blocked="partial"`` (D1)."""
    node = state.tree.node(ref.task_path)
    if node.parent is not None:
        return state.tree.node(node.parent).task.on_child_blocked == "partial"
    return ref.key is not None and node.task.on_child_blocked == "partial"


def unknowns_in(state: RunState) -> tuple[UnknownEntry, ...]:
    """Every explicit unknown of the run, in pick order (module doc)."""
    out: list[UnknownEntry] = []
    interrupt = None if state.finalizing is None else state.finalizing.interrupt
    for ref in all_instances(state):
        if _under_voided(state, ref):
            continue
        if state.voided(ref):
            out.append(UnknownEntry(ref.instance, "not_checked", interrupt))
            continue
        blocker = state.blocker(ref)
        if blocker is not None and tolerated(state, ref):
            out.append(UnknownEntry(ref.instance, "unverified", blocker))
    return tuple(out)


def unknowns(view: LogView, tree: CompiledTree, run: str) -> tuple[UnknownEntry, ...]:
    """Every explicit unknown of ``run`` in ``view`` (module doc). Pure."""
    return unknowns_in(run_state(view, tree, run))


def unknowns_at_end(state: RunState) -> tuple[UnknownEntry, ...]:
    """:func:`unknowns_in`, plus each finalizer leaf the spent reserve left unfinished (D5)."""
    out = list(unknowns_in(state))
    if state.finalizing is None:
        return tuple(out)
    for ref in all_instances(state):
        if not state.tree.node(ref.task_path).is_leaf or state.voided(ref):
            continue
        if state.status(ref) not in SETTLED and state.exempt(ref.task_path):
            out.append(UnknownEntry(ref.instance, "not_checked", RESERVE_SPENT))
    return tuple(out)


def writes_below(tree: CompiledTree, path: str) -> frozenset[str]:
    """The full kind names the Task at ``path`` and every Task below it write."""
    out: set[str] = set()
    for node in tree.walk():
        if node.path == path or node.path.startswith(path + "/"):
            out.update(tree.kind(w) for w in node.task.writes)
    return frozenset(out)


def as_unknown(kind: str, entry: UnknownEntry, key: str | None) -> Unknown:
    """The ``Unknown`` a reader sees for ``entry``'s slot of ``kind``."""
    question = kind if key is None else f"{kind} for {key}"
    failed = entry.blocker if entry.reason == "unverified" else None
    return Unknown(question, entry.reason, failed_step=failed)


def unknown_slots(
    view: LogView, tree: CompiledTree, run: str, kind: str
) -> tuple[tuple[str | None, UnknownEntry], ...]:
    """The ``(key, entry)`` of every slot of full kind ``kind`` that holds an unknown."""
    out: list[tuple[str | None, UnknownEntry]] = []
    for entry in unknowns(view, tree, run):
        ref = InstanceRef.parse(run, entry.key)
        if kind in writes_below(tree, ref.task_path):
            out.append((ref.key, entry))
    return tuple(out)


def entries(
    view: LogView, tree: CompiledTree, instance: InstanceRef, kind: str
) -> Mapping[str | None, ArtifactVersion | Unknown]:
    """Every key of ``kind``: its current version, or its explicit unknown (D2, D4).

    An unknown takes the place of any version its key has: a blocked or voided writer's
    output is never read as a value. Keys are sorted as strings.
    """
    from aksha.runtime.tasks.view import TaskView  # deferred: the view calls this module

    full = tree.kind(kind)
    found: dict[str | None, ArtifactVersion | Unknown] = dict(
        TaskView(view, tree, instance).all_latest(full)
    )
    for key, entry in unknown_slots(view, tree, instance.run, full):
        found[key] = as_unknown(full, entry, key)
    ordered = sorted(found, key=lambda k: "" if k is None else str(k))
    return MappingProxyType({k: found[k] for k in ordered})


def unknown_reads(
    view: LogView, tree: CompiledTree, instance: InstanceRef
) -> tuple[tuple[str, str | None, UnknownEntry], ...]:
    """``(kind, key, entry)`` for each unknown slot ``instance``'s reads name (ADR-P08 D4).

    Only ``all`` and field reads, in read order; never the instance's own output.
    """
    out: list[tuple[str, str | None, UnknownEntry]] = []
    seen: set[tuple[str, str | None]] = set()
    for read in tree.reads(instance.task_path):
        if read.keys == "own":
            continue
        for key, entry in unknown_slots(view, tree, instance.run, read.kind):
            if entry.key == instance.instance or (read.kind, key) in seen:
                continue
            seen.add((read.kind, key))
            out.append((read.kind, key, entry))
    return tuple(out)
