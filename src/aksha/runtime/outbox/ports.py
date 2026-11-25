"""Protocols for the outbox (ADR-P03): ``SystemOfRecord`` and ``Authority``.

``SystemOfRecord`` is what an adapter for an outside system implements (the mock send and
form services, a remote repository); adapters live outside the core. ``Authority`` is the
re-check of a person's authority for one entry (an approval for this exact action, the
target still current), which the interrupt and invalidation primitives (P17, P05) supply.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from aksha.runtime.outbox.types import OutboxEntry, SystemReceipt
from aksha.runtime.projections.view import LogView


class SystemOfRecord(Protocol):
    """An outside system that keeps its own ledger of what it applied (v2: receiver)."""

    def ledger_id(self) -> str:
        """Which ledger answers; recorded on every ``outbox.completed``."""
        ...

    def apply(self, key: str, request: Mapping[str, Any]) -> SystemReceipt:
        """Apply once. A known key returns its existing ``SystemReceipt`` and changes nothing.

        Raise :class:`~aksha.runtime.outbox.errors.TransientEffectError` for a failure worth
        retrying, with a code the tool's step policy names in ``retry_on``.
        """
        ...

    def lookup(self, key: str, request: Mapping[str, Any]) -> SystemReceipt | None:
        """Did this effect land? None: it did not.

        The request lets the adapter check the system's own state (S1). Never called for
        an ``at_most_once`` entry.
        """
        ...


class Authority(Protocol):
    """Whether a person's authority still covers ``entry``: a refusal code, or None."""

    def __call__(self, entry: OutboxEntry, view: LogView, /) -> str | None:
        """``approval_required``, ``hash_mismatch``, ``target_invalidated``, or None."""
        ...
