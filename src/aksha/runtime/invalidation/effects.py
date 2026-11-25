"""Effects refused on stale targets (ADR-P05 D9). Pure.

No selection save, form write, packet save, approval request or handoff runs on a version
that is invalidated or dirty, nor on a pending revision no person has accepted. Each such
path asks :func:`effect_refusal` with the artifact refs it acts on. :func:`authority`
wraps it as the outbox's ``Authority`` port (ADR-P03), so the worker re-checks it before
each first call; the approval half of that port arrives with ADR-P17.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from aksha.runtime.invalidation.view import InvalidationView, invalidation_view
from aksha.runtime.outbox.ports import Authority
from aksha.runtime.outbox.types import OutboxEntry
from aksha.runtime.projections.view import LogView

TARGET_INVALIDATED = "target_invalidated"
TARGET_DIRTY = "target_dirty"
TARGET_PENDING_REVIEW = "target_pending_review"
TARGET_UNKNOWN = "target_unknown"


def effect_refusal(iv: InvalidationView, targets: Iterable[str]) -> str | None:
    """The refusal code for an effect on ``targets``, or None when every target is fresh."""
    for target in targets:
        if target in iv.unaccepted:
            return TARGET_PENDING_REVIEW
        state = iv.versions.get(target)
        if state is None:
            return TARGET_UNKNOWN
        if state.freshness == "invalidated":
            return TARGET_INVALIDATED
        if state.freshness == "dirty":
            return TARGET_DIRTY
    return None


def authority(targets_of: Callable[[OutboxEntry], Iterable[str]]) -> Authority:
    """An outbox ``Authority`` that refuses an entry whose targets are stale (D9)."""

    def check(entry: OutboxEntry, view: LogView, /) -> str | None:
        targets = tuple(targets_of(entry))
        if not targets:
            return None
        return effect_refusal(invalidation_view(view), targets)

    return check
