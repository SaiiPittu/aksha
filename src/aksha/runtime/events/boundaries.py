"""The closed list of transaction boundaries (U1 to U12) an event type may be appended in.

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
    Boundary(
        "U1",
        "start",
        "session",
        "command.accepted or command.refused, user.message, route.decided, run.started",
    ),
    Boundary(
        "U2a",
        "step_intent",
        "execution",
        "step.started, and the outbox.enqueued of an effectful tool call",
    ),
    Boundary("U2b", "dispatch", "execution", "step.dispatched (at_most_once tools only)"),
    Boundary("U2c", "attempt_failed", "execution", "step.failed or attempt.discarded"),
    Boundary(
        "U2",
        "step_result",
        "execution",
        "step.completed with the result's facts: read.recorded, excerpts.minted, "
        "excerpts.shown, artifact rows and artifact.saved, relation.saved, citation.bound, "
        "context.compacted, "
        "an interrupt.raised or an approval request; for an "
        "effectful tool call, the outbox.completed or outbox.abandoned it ends with",
    ),
    Boundary(
        "U3",
        "stop_decision",
        "stop_policy",
        "completion_check.evaluated, task.entered, budget facts, interrupt.voided, run.ended, "
        "agent.reply, stop.decided last",
    ),
    Boundary(
        "U4",
        "resolution",
        "session",
        "command.accepted or command.refused, interrupt.resolved, the answer's "
        "excerpts.minted, approvals, budget.granted, a route answer's route.decided "
        "and run.started, "
        "review outcomes, run.stop_requested, "
        "an outbox.enqueued the decision wants",
    ),
    Boundary(
        "U5", "outbox_enqueue", "outbox", "command.accepted or command.refused, outbox.enqueued"
    ),
    Boundary(
        "U6",
        "outbox_complete",
        "outbox",
        "one worker fact: outbox.claimed, outbox.failed, outbox.completed or outbox.abandoned",
    ),
    Boundary("U7", "recovery", "execution", "recovery.completed (the system owner)"),
    Boundary("U8", "source_change", "invalidation", "source.changed, query.reexecuted"),
    Boundary("U9", "rebuild_start", "invalidation", "run.started with cause rebuild"),
    Boundary("U10", "derived_save", "artifacts", "derived artifact versions, inside U2"),
    Boundary("U11", "clean_exit", "interfaces", "process.closed"),
    # ADR-P30 E4: the deletes of a subject's sealed values commit with this one event.
    Boundary("U12", "erasure", "erasure", "erasure.performed"),
)

BOUNDARIES: Mapping[str, Boundary] = MappingProxyType({b.id: b for b in _ALL})
"""Every transaction boundary by ID. Closed: extensions add event types, never boundaries."""


def boundary(boundary_id: str) -> Boundary:
    """The boundary named ``boundary_id``, or :class:`UnknownBoundary`."""
    try:
        return BOUNDARIES[boundary_id]
    except KeyError:
        raise UnknownBoundary(message=f"no transaction boundary {boundary_id!r}") from None
