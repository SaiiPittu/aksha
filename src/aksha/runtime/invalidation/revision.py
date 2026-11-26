"""Pending revisions and the diff review they raise (ADR-P05 D1, D5). Pure.

A rebuilt human-reviewed version is never made current by its writer. The writer saves a
pending revision through the artifact store's one save path, and in the same step result
(U2) :func:`review_raised` adds the ``interrupt.raised{kind: review}`` that shows a person the
revision beside the old version: the reads that changed, the lineage chain back to the
change, a field-level redline and the card's hash. Readers keep seeing the old version
(``versions.current``) until the person accepts. A revision equal in content still gets
its review, led by the changed reads, because the person decided on evidence that changed
(D5, P05-2).
"""

from __future__ import annotations

from collections.abc import Mapping

from aksha.runtime.artifacts.types import ArtifactKind, ArtifactVersion
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.invalidation.redline import redline
from aksha.runtime.invalidation.reviews import REVIEW, card_hash
from aksha.runtime.invalidation.types import DiffReview, VersionState


def diff_review(
    old: ArtifactVersion,
    old_state: VersionState,
    revision: ArtifactVersion,
    kind: ArtifactKind,
    interrupt_id: str,
    *,
    run: str | None = None,
    instance: str | None = None,
) -> DiffReview:
    """The diff review of ``revision`` against ``old`` (a human-reviewed version)."""
    if old.ref.slot() != revision.ref.slot() or revision.ref.version <= old.ref.version:
        raise ValueError(f"{revision.ref} is not a later version of {old.ref}'s slot")
    if not old_state.human_reviewed:
        raise ValueError(f"{old.ref} is machine-only; it regenerates with no review")
    changed = old_state.changed_reads()
    hunks = redline(kind, old.content, revision.content)
    card = card_hash(old.ref, revision.ref, changed, old_state.via, hunks)
    return DiffReview(
        interrupt_id, old.ref, revision.ref, changed, old_state.via, hunks, card, run, instance
    )


def review_raised(review: DiffReview, *, step: str | None = None) -> NewEvent:
    """The ``interrupt.raised{kind: review}`` fact that commits with the revision's save."""
    payload: Mapping[str, object] = {
        "interrupt": review.interrupt_id,
        "kind": REVIEW,
        "old": str(review.old),
        "revision": str(review.revision),
        "card_hash": review.card_hash,
        "changed_reads": [r.as_json() for r in review.changed_reads],
        "via": list(review.via),
        "hunks": [h.as_json() for h in review.hunks],
    }
    return NewEvent(
        "interrupt.raised", payload, run=review.run, instance=review.instance, step=step
    )
