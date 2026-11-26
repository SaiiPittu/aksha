"""Lineage: recorded reads (``read_recorded``, ``recorded_reads``) and the graph (``lineage``).

ADR-P04 D6: lineage is captured, never declared. A read enters lineage only as a
``read.recorded`` fact, which the runtime writes when context assembly renders a version to
a Task instance (``via="context"``) or a tool returns one to it (``via="tool"``), in the
step's result transaction (U2). No tool body and no model writes it: a tool returns
values, and the runtime records what they were. A save then takes every read the
instance recorded, never a subset; ``derived_from`` edges are exactly those reads, so
``test_lineage_equals_recorded_reads`` holds by construction.

ADR-P04's ``ReadRecorder`` Protocol becomes these two functions: the record is a fact
in the event store and the read set is a projection over it, so there is nothing outside
the runtime to plug in.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.types import ArtifactRef, InputRef, LineageGraph
from aksha.runtime.artifacts.versions import all_versions
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.projections.view import LogView

Via = Literal["context", "tool"]


def read_recorded(
    read: InputRef, *, run: str, instance: str, step: str | None = None, via: Via = "tool"
) -> NewEvent:
    """The ``read.recorded`` fact for one lineage input of ``instance`` in ``run``."""
    if not run or not instance:
        raise ValueError("a read is recorded for one Task instance of one run")
    payload = {"input": read.as_json(), "via": via}
    return NewEvent("read.recorded", payload, run=run, instance=instance, step=step)


def reads_to_record(
    view: LogView, reads: Iterable[InputRef], *, run: str, instance: str, step: str | None = None,
    via: Via = "tool",
) -> tuple[NewEvent, ...]:  # fmt: skip
    """The ``read.recorded`` facts for those of ``reads`` the instance has not recorded yet."""
    seen = recorded_reads(view, run, instance)
    out: list[NewEvent] = []
    for r in reads:
        if r not in seen:
            seen = seen | {r}
            out.append(read_recorded(r, run=run, instance=instance, step=step, via=via))
    return tuple(out)


def recorded_reads(view: LogView, run: str, instance: str) -> frozenset[InputRef]:
    """Every lineage input ``instance`` recorded in ``run``, up to the view's ``seq_read``.

    Only the reads since the instance's last re-entry for a rebuild count
    (``task.entered{cause: rebuild}``, read by name; ADR-P05 D7): an instance that
    regenerates its work in place starts a new generation of reads, so the versions it
    read before the change are not lineage of what it saves now.
    """
    since = 0
    for e in view.events:
        if (
            e.type == "task.entered"
            and e.run == run
            and e.instance == instance
            and e.payload.get("cause") == "rebuild"
        ):
            since = e.seq
    return frozenset(
        InputRef.from_json(e.payload["input"])
        for e in view.events
        if e.type == "read.recorded" and e.run == run and e.instance == instance and e.seq > since
    )


def lineage(
    view: LogView,
    ref: ArtifactRef,
    *,
    direction: Literal["up", "down"] = "up",
    depth: int | None = None,
    blobs: BlobStore | None = None,
) -> LineageGraph:
    """The lineage of ``ref``: what it was built from (``up``) or what was built from it.

    ``depth`` bounds how many edges are followed from ``ref`` (None: all). Upward, every
    lineage input is a node and only artifact inputs are followed further; downward, the
    nodes are the versions that read ``ref``, then the versions that read those. A
    path-scoped read (S3) is drawn as an edge from the whole version.
    """
    by_input = {v.ref.as_input(): v for v in all_versions(view, blobs=blobs)}
    root = ref.as_input()
    nodes: set[InputRef] = {root}
    edges: set[tuple[InputRef, InputRef]] = set()
    frontier, level = [root], 0
    while frontier and (depth is None or level < depth):
        nxt: list[InputRef] = []
        for node in frontier:
            if direction == "up":
                found = by_input.get(node)
                pairs = [(_whole(r), node) for r in found.reads] if found else []
                reached = [a for a, _ in pairs]
            else:
                readers = [
                    v.ref.as_input()
                    for v in by_input.values()
                    if node in {_whole(r) for r in v.reads}
                ]
                pairs = [(node, r) for r in readers]
                reached = readers
            edges.update(pairs)
            nxt.extend(n for n in reached if n not in nodes)
            nodes.update(reached)
        frontier, level = nxt, level + 1
    return LineageGraph(root, frozenset(nodes), frozenset(edges))


def _whole(read: InputRef) -> InputRef:
    """``read`` without its path: the version a path-scoped read is part of."""
    return read if read.path is None else InputRef(read.kind, read.id, read.version)
