"""Effect values (ADR-P03): ``SystemOfRecordSpec`` and ``EffectSpec``.

``SystemReceipt`` and ``OutboxEntry`` arrive with the transactional outbox.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from aksha.runtime.execution.types import StepSemantics


@dataclass(frozen=True)
class SystemOfRecordSpec:
    """A system-of-record adapter an extension contributes (ADR-P20 S2): a name and a factory.

    The factory builds a ``SystemOfRecord`` (``outbox/ports.py``) once that port is built.
    """

    name: str
    factory: Callable[..., object]


@dataclass(frozen=True)
class EffectSpec:
    """How an ``external`` tool's effect is keyed and finished after a crash (ADR-P03, P10 S1).

    ``system`` names a system of record, or None for a tool whose system cannot be asked.
    ``key_fields`` are JSON pointers into the arguments; the idempotency key is computed
    from them. ``semantics`` uses ADR-P02's names; ``reconcile`` needs a ``system``.
    """

    system: str | None
    key_fields: tuple[str, ...]
    semantics: StepSemantics
    on_unknown: Literal["tell_model", "interrupt"] = "tell_model"
    record_fields: tuple[str, ...] = ()
    receipt_kind: str | None = None
