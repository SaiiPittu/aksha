"""The outbox projection and ``enqueue`` (ADR-P03 D1): an entry commits with its decision.

``Outbox`` is a pure reducer over the ``outbox.*`` events; ``entries`` and
``pending_outbox`` read it. ``enqueue`` builds an entry and its ``outbox.enqueued`` event
for the caller to append in its own transaction (the command that decided the effect, or the
step intent of an effectful tool call), after re-checking authority. An entry for a key
already enqueued is returned as it stands, with no event: the effect it means is already
decided, and a rebuild run's equal request reaches the first entry and its receipt instead
of a second effect.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from aksha.runtime.canonical import freeze
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution.types import StepId
from aksha.runtime.outbox import facts
from aksha.runtime.outbox.errors import OutboxRefused
from aksha.runtime.outbox.keys import (
    Resolve,
    artifact_sources,
    idempotency_key,
    key_values,
    record_key,
    record_values,
    request_hash,
)
from aksha.runtime.outbox.ports import Authority
from aksha.runtime.outbox.types import OUTCOME_UNKNOWN, EffectSpec, OutboxEntry, OutboxState
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.view import ArtifactsView, LogView

OutboxEntries = Mapping[str, OutboxEntry]
"""Idempotency key to entry, in enqueue order; frozen."""


class Outbox:
    """Reducer over the ``outbox.*`` events, keyed by idempotency key."""

    name = "outbox"
    reads = facts.TYPES

    def initial(self, args: Hashable) -> OutboxEntries:
        """No entries."""
        return MappingProxyType({})

    def apply(self, state: OutboxEntries, event: Event, artifacts: ArtifactsView) -> OutboxEntries:
        """Add an entry on ``outbox.enqueued``; move it on through the worker's facts."""
        p = event.payload
        key = str(p["key"])
        if event.type == "outbox.enqueued":
            return MappingProxyType({**state, key: _entry(event)})
        entry = state.get(key)
        if entry is None:
            return state  # a worker fact with no entry: nothing to move
        attempt = int(p.get("attempt", p.get("attempts", entry.attempts)))
        attempts = max(entry.attempts, attempt)
        if event.type == "outbox.claimed":
            entry = replace(entry, state="dispatched", attempts=attempts)
        elif event.type == "outbox.failed":
            entry = replace(entry, attempts=attempts)
        elif event.type == "outbox.completed":
            entry = replace(entry, state="completed", attempts=attempts, receipt=p["receipt"],
                            ledger_id=str(p["ledger_id"]))  # fmt: skip
        else:
            reason = str(p["reason"])
            state_name: OutboxState = (
                "outcome_unknown" if reason == OUTCOME_UNKNOWN else "abandoned"
            )
            on_unknown = p.get("on_unknown", entry.on_unknown)
            entry = replace(entry, state=state_name, attempts=attempts, reason=reason,
                            on_unknown=on_unknown)  # fmt: skip
        return MappingProxyType({**state, key: entry})


def _entry(event: Event) -> OutboxEntry:
    p = event.payload
    approval, command = p.get("approval_id"), p.get("command_id")
    record = p.get("record")
    return OutboxEntry(
        key=str(p["key"]),
        owner=event.owner,
        tool=str(p["tool"]),
        system=None if p["system"] is None else str(p["system"]),
        semantics=p["semantics"],
        on_unknown=p["on_unknown"],
        request=p["request"],
        request_hash=str(p["request_hash"]),
        seq=event.seq,
        approval_id=None if approval is None else str(approval),
        command_id=None if command is None else str(command),
        run=event.run,
        step=event.step,
        record_key=None if record is None else str(record["key"]),
        record=None if record is None else record["fields"],
        sources=tuple(str(s) for s in p.get("sources", ())),
    )


OUTBOX = Outbox()
"""The one instance the runtime uses."""


def entries(view: LogView) -> OutboxEntries:
    """Every outbox entry of the view's owner, in enqueue order. Pure."""
    return rebuild(OUTBOX, view)


def pending_outbox(view: LogView) -> tuple[OutboxEntry, ...]:
    """The entries with no outcome yet (``enqueued`` or ``dispatched``), in order. Pure."""
    return tuple(e for e in entries(view).values() if e.state in ("enqueued", "dispatched"))


@dataclass(frozen=True)
class Enqueued:
    """What ``enqueue`` decided: the entry, and the event to append (None: already there)."""

    entry: OutboxEntry
    event: NewEvent | None


def enqueue(
    view: LogView,
    tool: str,
    args: Mapping[str, Any],
    spec: EffectSpec,
    *,
    approval_id: str | None = None,
    command_id: str | None = None,
    session: str | None = None,
    run: str | None = None,
    step: StepId | None = None,
    resolve: Resolve | None = None,
    authority: Authority | None = None,
) -> Enqueued:
    """Build ``tool``'s outbox entry for ``args``; the caller appends ``event`` with its decision.

    ``step`` is the tool step whose intent the entry commits with (``StepExecutor.effect_step``);
    a command's entry has none.

    A tool that declares ``record_fields`` also records which outside record the request
    revises and the artifact versions among its key values (ADR-P29 D1).

    Raises ``KeyFieldMissing`` when a key or record field is absent and :class:`OutboxRefused` with
    the authority's code when it no longer covers the entry. Pure: nothing is appended.
    """
    key = idempotency_key(tool, view.owner, args, spec, resolve=resolve)
    existing = entries(view).get(key)
    if existing is not None:
        return Enqueued(existing, None)
    record = record_values(args, spec) if spec.record_fields else None
    entry = OutboxEntry(
        key=key,
        owner=view.owner,
        tool=tool,
        system=spec.system,
        semantics=spec.semantics,
        on_unknown=spec.on_unknown,
        request=freeze(args),
        request_hash=request_hash(args),
        seq=0,
        approval_id=approval_id,
        command_id=command_id,
        run=step.run if step is not None else run,
        step=None if step is None else str(step),
        record_key=None if record is None else record_key(tool, view.owner, args, spec),
        record=None if record is None else freeze(record),
        sources=() if record is None else artifact_sources(key_values(args, spec, resolve)),
    )
    if authority is not None:
        code = authority(entry, view)
        if code is not None:
            raise OutboxRefused(code, f"{tool}: {code}")
    instance = None if step is None else step.instance
    return Enqueued(entry, facts.enqueued(entry, session=session, instance=instance))
