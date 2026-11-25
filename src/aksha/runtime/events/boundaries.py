"""The closed list of transaction boundaries (U1 to U11) an event type may be appended in.

A boundary is one commit unit: every event (and artifact row) in it commits in one
transaction or not at all (ADR-P01 D5). The list carries v2 05a section 2.4 forward under
the ADR-P01 D8 names; nothing outside this list calls ``EventStore.append``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from aksha.runtime.events.errors import UnknownBoundary


@dataclass(frozen=True)
class Boundary:
    """One transaction boundary: its ID, its name and the module that writes it."""

    id: str
    name: str
    written_by: str
    contents: str


_ALL = (
    Boundary("U1", "start", "session", "user.message, run.started"),
    Boundary("U2a", "step_intent", "execution", "step.started"),
    Boundary("U2b", "dispatch", "execution", "step.dispatched (at_most_once tools only)"),
    Boundary("U2c", "attempt_failed", "execution", "step.failed or attempt.discarded"),
    Boundary(
        "U2",
        "step_result",
        "execution",
        "step.completed with the result's facts: artifact rows and artifact.saved, "
        "relations, an interrupt.raised or an approval request",
    ),
    Boundary(
        "U3",
        "stop_decision",
        "stop_policy",
        "completion_check.evaluated, task.entered, budget facts, run.ended, "
        "agent.reply, stop.decided last",
    ),
    Boundary("U4", "resolution", "session", "interrupt.resolved, approvals, review outcomes"),
    Boundary("U5", "outbox_enqueue", "outbox", "outbox.enqueued"),
    Boundary("U6", "outbox_complete", "outbox", "outbox.completed or outbox.abandoned"),
    Boundary("U7", "recovery", "execution", "recovery.completed (the system owner)"),
    Boundary("U8", "source_change", "invalidation", "source.changed, query.reexecuted"),
    Boundary("U9", "rebuild_start", "invalidation", "run.started with cause rebuild"),
    Boundary("U10", "derived_save", "artifacts", "derived artifact versions, inside U2"),
    Boundary("U11", "clean_exit", "interfaces", "process.closed"),
)

BOUNDARIES: Mapping[str, Boundary] = MappingProxyType({b.id: b for b in _ALL})
"""Every transaction boundary by ID. Closed: extensions add event types, never boundaries."""


def boundary(boundary_id: str) -> Boundary:
    """The boundary named ``boundary_id``, or :class:`UnknownBoundary`."""
    try:
        return BOUNDARIES[boundary_id]
    except KeyError:
        raise UnknownBoundary(message=f"no transaction boundary {boundary_id!r}") from None
