"""Effect values (ADR-P03): the effect and system specs, the system receipt, the outbox entry.

``OutboxEntry`` is a projection over the ``outbox.*`` events (``outbox/outbox.py``); the
event store is the outbox table, so an entry is never stored anywhere else.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from aksha.runtime.execution.types import OnUnknown, StepSemantics


@dataclass(frozen=True)
class SystemOfRecordSpec:
    """A system-of-record adapter an extension contributes (ADR-P20 S2): a name and a factory.

    The factory builds a :class:`~aksha.runtime.outbox.ports.SystemOfRecord`.
    """

    name: str
    factory: Callable[..., object]


@dataclass(frozen=True)
class EffectSpec:
    """How an ``external`` tool's effect is keyed and finished after a crash (ADR-P03, P10 S1).

    ``system`` names a system of record, or None for a tool whose system cannot be asked
    (the worker then calls the system registered under the tool's own name).
    ``key_fields`` are JSON pointers into the arguments; the idempotency key is computed
    from them. ``semantics`` uses ADR-P02's names; ``reconcile`` needs a ``system``.
    """

    system: str | None
    key_fields: tuple[str, ...]
    semantics: StepSemantics
    on_unknown: Literal["tell_model", "interrupt"] = "tell_model"
    record_fields: tuple[str, ...] = ()
    receipt_kind: str | None = None


@dataclass(frozen=True)
class SystemReceipt:
    """A system of record's answer for one key: which ledger answered and its own reference.

    ``body`` is JSON data, such as a message ID or a commit sha; it is recorded on
    ``outbox.completed`` and returned to every later caller with the same key.
    """

    key: str
    ledger_id: str
    body: Mapping[str, Any]


OutboxState = Literal["enqueued", "dispatched", "completed", "abandoned", "outcome_unknown"]
"""Where an entry stands. ``dispatched``: claimed, and no outcome is recorded yet."""

OUTCOME_UNKNOWN = "outcome_unknown"
"""The reason of an abandonment whose effect may have landed: it cannot be asked (D4)."""

RETRIES_EXHAUSTED = "retries_exhausted"
"""The reason of an abandonment after the step policy's retries are used up (D8)."""

EFFECT_EXCEPTION = "effect_exception"
"""The reason of an abandonment after the system raised something other than a refusal."""

RUN_STOPPED = "run_stopped"
"""The reason of an abandonment of a step's entry whose run a person stopped (ADR-P18 D5)."""


@dataclass(frozen=True)
class OutboxEntry:
    """One outbox entry as its events left it.

    ``seq`` is the ``outbox.enqueued`` event's sequence number, the order the worker runs
    entries in. ``attempts`` is the highest claimed attempt, so a restart never resets it.
    ``receipt`` and ``ledger_id`` are set when completed; ``reason`` when abandoned or
    unknown. ``run`` and ``step`` name the tool step whose intent enqueued the entry (None
    for a command's entry, such as a handoff); the worker commits that step's result with
    the entry's end. ``record_key`` and ``record`` name the outside record the request
    revises when its tool declares ``record_fields``, and ``sources`` the artifact versions
    it was written from (ADR-P29 D1); both are recorded on ``outbox.enqueued``.
    """

    key: str
    owner: str
    tool: str
    system: str | None
    semantics: StepSemantics
    on_unknown: OnUnknown
    request: Mapping[str, Any]
    request_hash: str
    seq: int
    approval_id: str | None = None
    command_id: str | None = None
    state: OutboxState = "enqueued"
    attempts: int = 0
    receipt: Mapping[str, Any] | None = None
    ledger_id: str | None = None
    reason: str | None = None
    run: str | None = None
    step: str | None = None
    record_key: str | None = None
    record: Mapping[str, Any] | None = None
    sources: tuple[str, ...] = ()

    @property
    def target(self) -> str:
        """The name of the system the worker calls: ``system``, or the tool's own name."""
        return self.system if self.system is not None else self.tool
