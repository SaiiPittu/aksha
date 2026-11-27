"""``TaskView``: what a completion check may read, the run view narrowed to one instance.

A completion check is pure (ADR-P15 D2, PATTERNS P-3): it reads only this view. The view
holds a ``LogView`` (no store handle, so nothing that reads it can write), the compiled
tree and the instance; it has no clock, no model and no network. Its readers:

1. ``latest(kind, key)`` and ``own(kind)``: the current version of a slot, skipping a
   pending revision no person accepted (ADR-P05 D1), so "done" is judged on the artifacts
   readers see now, never on a stale or proposed version.
2. ``all_latest(kind)``: the current version of every key of a keyed kind.
3. ``answers()``: the resolved interrupts this instance raised (ADR-P17).
4. ``child_status(task_id)``: the statuses of a child Task's instances, by key.
5. ``freshness(ref)``: a version's state in the invalidation view (ADR-P05), so a check
   that reads a tool's report can refuse a report the files moved past (D4).
6. ``called(tool)``: how many calls of ``tool`` this instance completed, so a forced
   first call (ADR-P09 D3's ``recall``) holds only until it is made.
8. ``hidden``: versions another run's line saved for this instance's writer that this run
   did not reuse (ADR-P04 R1, ``tasks.reuse``); ``latest``, ``own`` and ``all_latest``
   skip them, so an earlier run's work does not pass this run's check unexamined.
7. ``entries(kind)``: every key of a kind, its current version or, where its writer was
   blocked under a partial rule or voided by finalization, an explicit ``Unknown``
   (ADR-P24 D2, D4). A check that refuses unknowns still refuses them (D3).

Kind names may be local (``line``); the view resolves them in the agent's namespace.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from aksha.runtime.artifacts.types import ArtifactVersion
from aksha.runtime.artifacts.versions import current, unaccepted_revisions, versions
from aksha.runtime.grounding.types import Unknown
from aksha.runtime.interrupts.reducers import INTERRUPT_RAISED, INTERRUPT_RESOLVED
from aksha.runtime.invalidation.types import Freshness
from aksha.runtime.invalidation.view import invalidation_view
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import InstanceRef, InstanceStatus


@dataclass(frozen=True)
class TaskView:
    """The read-only view one completion check is given (module doc)."""

    log: LogView
    tree: CompiledTree
    instance: InstanceRef
    hidden: frozenset[str] = frozenset()

    @property
    def key(self) -> str | None:
        """This instance's key, or None for an un-keyed Task."""
        return self.instance.key

    def latest(self, kind: str, key: str | None = None) -> ArtifactVersion | None:
        """The current version of slot ``(kind, key)``, or None while it is empty."""
        if not self.hidden:
            return current(self.log, self.tree.kind(kind), key)
        pending = unaccepted_revisions(self.log)
        found = [
            v
            for v in versions(self.log, self.tree.kind(kind), key)
            if str(v.ref) not in pending and str(v.ref) not in self.hidden
        ]
        return found[-1] if found else None

    def own(self, kind: str) -> ArtifactVersion | None:
        """The current version of ``kind`` at this instance's own key."""
        return self.latest(kind, self.key)

    def all_latest(self, kind: str) -> Mapping[str | None, ArtifactVersion]:
        """The current version of every key of ``kind``, keys sorted as strings."""
        full = self.tree.kind(kind)
        pending = unaccepted_revisions(self.log)
        keys = {
            e.payload.get("key")
            for e in self.log.events
            if e.type == "artifact.saved" and e.payload.get("kind") == full
        }
        out: dict[str | None, ArtifactVersion] = {}
        for key in sorted(keys, key=lambda k: "" if k is None else str(k)):
            found = [
                v
                for v in versions(self.log, full, key)
                if str(v.ref) not in pending and str(v.ref) not in self.hidden
            ]
            if found:
                out[key] = found[-1]
        return MappingProxyType(out)

    def answers(self) -> tuple[Mapping[str, Any], ...]:
        """The ``interrupt.resolved`` payloads of interrupts this instance raised, in order."""
        mine = {
            str(e.payload.get("interrupt"))
            for e in self.log.events
            if e.type == INTERRUPT_RAISED
            and e.run == self.instance.run
            and e.instance == self.instance.instance
        }
        return tuple(
            e.payload
            for e in self.log.events
            if e.type == INTERRUPT_RESOLVED and str(e.payload.get("interrupt")) in mine
        )

    def child_status(self, task_id: str) -> Mapping[str | None, InstanceStatus]:
        """The statuses of child Task ``task_id``'s instances under this one, by key."""
        from aksha.runtime.tasks.status import run_state  # deferred: status builds views

        state = run_state(self.log, self.tree, self.instance.run)
        child = self.instance.child(task_id)
        self.tree.node(child.task_path)
        return MappingProxyType({child.key: state.status(child)})

    def entries(self, kind: str) -> Mapping[str | None, ArtifactVersion | Unknown]:
        """Every key of ``kind``: its current version, or its explicit unknown (ADR-P24)."""
        from aksha.runtime.finalization.unknowns import entries  # deferred: it reads statuses

        return entries(self.log, self.tree, self.instance, kind)

    def freshness(self, ref: str) -> Freshness | None:
        """The version's state in the invalidation view, or None when the view lacks it."""
        iv = invalidation_view(self.log)
        found = iv.versions.get(ref)
        return None if found is None else found.freshness

    def called(self, tool: str) -> int:
        """How many calls of ``tool`` this instance completed with outcome ``ok``."""
        return sum(
            1
            for e in self.log.events
            if e.type == "step.completed"
            and e.run == self.instance.run
            and e.instance == self.instance.instance
            and e.payload.get("tool") == tool
            and e.payload.get("outcome") == "ok"
        )
