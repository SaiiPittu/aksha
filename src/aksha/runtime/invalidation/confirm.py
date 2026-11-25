"""What a commit confirms (ADR-P05 D9): revocations and stale reviews, in the same transaction.

An approval is revoked only when its target is confirmed invalidated (an input came back
different), and in the transaction that confirmed it, so a change that comes out equal
revokes nothing. The confirming transaction is a source change (U8), a step result that
saved a rebuilt version (U2) or a person's acceptance (U4). The caller hands
:func:`confirmations` the events it is about to commit; it projects the log as it will be
after them and returns the ``approval.revoked`` facts, and an ``interrupt.voided`` for
every open diff review whose pending revision went stale (``revision_invalidated``), to
append in the same boundary. Pure.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from aksha.runtime.artifacts.types import WriterId
from aksha.runtime.canonical import freeze
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.invalidation.reviews import approvals, open_reviews
from aksha.runtime.invalidation.view import InvalidationView, invalidation_view
from aksha.runtime.projections.view import ArtifactsView, LogView
from aksha.runtime.storage.rows import ArtifactRow, StoredArtifact

REVISION_INVALIDATED = "revision_invalidated"
"""The ``interrupt.voided`` reason of a diff review whose pending revision went stale."""


def after(view: LogView, events: Sequence[NewEvent], rows: Sequence[ArtifactRow] = ()) -> LogView:
    """``view`` as it will read once ``events`` and ``rows`` commit. Never cached.

    The new events get the sequence numbers after ``view.seq_read``; each row is placed at
    its ``artifact.saved`` event. The result is not complete (``LogView.complete``), so
    the projection cache never keeps a state computed from it.
    """
    base = view.seq_read
    stored: list[Event] = []
    for i, e in enumerate(events, start=1):
        stored.append(
            Event(
                type=e.type,
                payload=freeze(dict(e.payload)),
                run=e.run,
                instance=e.instance,
                step=e.step,
                session=e.session,
                dedup_key=e.dedup_key,
                seq=base + i,
                owner=view.owner,
                at="",
                boot=0,
                schema_version=1,
            )
        )
    at = {str(e.payload.get("ref")): e.seq for e in stored if e.type == "artifact.saved"}
    added = tuple(StoredArtifact(view.owner, at[r.ref], r) for r in rows if r.ref in at)
    seq = base + len(stored)
    artifacts = ArtifactsView((*view.artifacts.all(), *added), seq)
    return LogView(view.owner, seq, (*view.events, *stored), artifacts, complete=False)


def confirmations(
    view: LogView,
    events: Sequence[NewEvent],
    rows: Sequence[ArtifactRow] = (),
    *,
    withdrawn: Iterable[WriterId] = (),
) -> tuple[NewEvent, ...]:
    """The revocations and review voids that ``events`` confirm (module doc)."""
    future = after(view, events, rows)
    iv = invalidation_view(future, withdrawn=withdrawn)
    return (*revocations(future, iv), *stale_reviews(future, iv))


def revocations(view: LogView, iv: InvalidationView) -> tuple[NewEvent, ...]:
    """``approval.revoked`` for each live approval with an invalidated target; never a used one."""
    out: list[NewEvent] = []
    for a in approvals(view):
        if a.revoked or a.used:
            continue
        for target in a.targets:
            state = iv.versions.get(target)
            if state is not None and state.freshness == "invalidated":
                moved = [r.input.as_json() for r in state.changed_reads()]
                payload = {"approval": a.approval, "target": target, "moved": moved}
                out.append(NewEvent("approval.revoked", payload))
                break
    return tuple(out)


def stale_reviews(view: LogView, iv: InvalidationView) -> tuple[NewEvent, ...]:
    """``interrupt.voided`` for each open diff review whose revision is now stale."""
    out: list[NewEvent] = []
    for review in open_reviews(view).values():
        state = iv.versions.get(str(review.revision))
        if state is not None and state.stale:
            payload = {"interrupt": review.interrupt, "reason": REVISION_INVALIDATED}
            out.append(NewEvent("interrupt.voided", payload, run=review.run))
    return tuple(out)
