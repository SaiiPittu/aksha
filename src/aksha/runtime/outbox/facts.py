"""The outbox facts, built in one place, with their boundaries and dedup keys. Pure.

====================  ========  ======================================================
Event                 Boundary  When
====================  ========  ======================================================
``outbox.enqueued``   U5 or U4  with the command that decided the effect (D1)
``outbox.enqueued``   U2a       with the ``step.started`` of an effectful tool call (D1)
``outbox.claimed``    U6        alone, before the call; one claim per attempt
``outbox.failed``     U6        a failed attempt, with whether it is retried (D8)
``outbox.completed``  U6 or U2  the system's receipt; U2 with a step's ``step.completed``
``outbox.abandoned``  U6 or U2  refused, retries exhausted, stopped, or outcome unknown
====================  ========  ======================================================

The claim's dedup key (``oclaim:{key}/a{attempt}``) is what makes it exclusive: two
workers racing on one entry, in one process or several, lose to the unique index.
``outbox.completed`` and ``outbox.abandoned`` share ``done-outbox:{key}``, so an entry
ends once.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.canonical import thaw
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.types import OnUnknown
from aksha.runtime.outbox.types import OutboxEntry

ENQUEUE = "U5"
"""The boundary of a command whose only effect is an outbox entry (a handoff)."""

WORKER = "U6"
"""The boundary of each worker fact."""

TYPES = frozenset(
    {"outbox.enqueued", "outbox.claimed", "outbox.failed", "outbox.completed", "outbox.abandoned"}
)
"""Every outbox event type, for reads and the projection."""


def entry_key(key: str) -> str:
    """The dedup key of an entry's ``outbox.enqueued``."""
    return f"outbox:{key}"


def claim_key(key: str, attempt: int) -> str:
    """The dedup key of an attempt's claim."""
    return f"oclaim:{key}/a{attempt}"


def done_key(key: str) -> str:
    """The dedup key an entry's completion and abandonment share."""
    return f"done-outbox:{key}"


def enqueued(
    entry: OutboxEntry,
    *,
    session: str | None = None,
    run: str | None = None,
    instance: str | None = None,
) -> NewEvent:
    """The outbox entry itself: the request, its key and how to finish it after a crash.

    A step's entry carries the step in its envelope (``entry.step``), so the worker can
    commit the step's result with the entry's end.
    """
    payload: dict[str, Any] = {
        "key": entry.key,
        "tool": entry.tool,
        "system": entry.system,
        "semantics": entry.semantics,
        "on_unknown": entry.on_unknown,
        "request": thaw(entry.request),
        "request_hash": entry.request_hash,
    }
    if entry.approval_id is not None:
        payload["approval_id"] = entry.approval_id
    if entry.command_id is not None:
        payload["command_id"] = entry.command_id
    run = entry.run if entry.run is not None else run
    return NewEvent(
        "outbox.enqueued", payload, session=session, run=run, instance=instance, step=entry.step
    )


def claimed(key: str, attempt: int) -> NewEvent:
    """The claim of ``attempt``: past it the call may have reached the system."""
    return NewEvent("outbox.claimed", {"key": key, "attempt": attempt})


def failed(key: str, attempt: int, code: str, *, retried: bool, backoff_ms: int) -> NewEvent:
    """A failed attempt; ``retried`` says another follows after ``backoff_ms``."""
    payload = {"key": key, "attempt": attempt, "code": code, "retried": retried,
               "backoff_ms": backoff_ms}  # fmt: skip
    return NewEvent("outbox.failed", payload)


def completed(
    key: str,
    ledger_id: str,
    receipt: Mapping[str, Any],
    *,
    attempts: int,
    reconciled: bool = False,
) -> NewEvent:
    """The system's receipt. ``reconciled``: read back by ``lookup`` after a restart."""
    payload: dict[str, Any] = {
        "key": key,
        "ledger_id": ledger_id,
        "receipt": thaw(receipt),
        "attempts": attempts,
    }
    if reconciled:
        payload["reconciled"] = True
    return NewEvent("outbox.completed", payload)


def abandoned(
    key: str, reason: str, *, attempts: int, on_unknown: OnUnknown | None = None
) -> NewEvent:
    """The entry ends without a receipt. ``on_unknown`` rides on an ``outcome_unknown``."""
    payload: dict[str, Any] = {"key": key, "reason": reason, "attempts": attempts}
    if on_unknown is not None:
        payload["on_unknown"] = on_unknown
    return NewEvent("outbox.abandoned", payload)
