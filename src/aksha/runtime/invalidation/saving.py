"""What a step that saves commits beside its saves (ADR-P05 D1, D9): one function per rule.

The agent loop's save path (``loop/calls.py``) and every other step that saves artifacts
calls these, so a rebuild made through the runtime does what the design says, in the
step's own transaction (U2):

1. :func:`save_origin`: a writer that rebuilds a version a person chose saves its new
   version with origin ``copy_choice`` (the plan's mode for it, D7), so the revision is
   still the person's choice and goes to a person again.
2. :func:`review_facts`: a save that replaces a human-reviewed version while that
   version is changing (invalidated or dirty) is a pending revision, never current: the
   same result raises its diff review (``interrupt.raised{kind: review}``), so readers see
   the old version until a person accepts (D1). An equal revision still gets its review,
   led by the changed reads (D5). A version saved from the person's own answer (origin
   ``choice``) raises none: the person decided it just now (ADR-P17 D2).
3. :func:`step_confirmations`: the reviews above, then the revocations and review voids
   the step's facts confirm (``confirm.confirmations``, D9), to append with them.

Pure: each reads the log as the step saw it and the facts it is about to commit.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from aksha.runtime.artifacts.types import ArtifactKind, ArtifactVersion, Origin
from aksha.runtime.artifacts.versions import current
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.invalidation.confirm import confirmations
from aksha.runtime.invalidation.revision import diff_review, review_raised
from aksha.runtime.invalidation.view import HUMAN_ORIGINS, InvalidationView, invalidation_view
from aksha.runtime.projections.view import LogView
from aksha.runtime.storage.rows import ArtifactRow


def save_origin(view: LogView, kind: str, key: str | None) -> Origin:
    """``copy_choice`` when the slot's current version is a person's choice and is changing."""
    old = current(view, kind, key)
    if old is None or old.origin not in HUMAN_ORIGINS:
        return "task"
    state = invalidation_view(view).versions.get(str(old.ref))
    return "copy_choice" if state is not None and state.stale else "task"


def review_facts(
    view: LogView,
    iv: InvalidationView,
    saved: Sequence[ArtifactVersion],
    kinds: Mapping[str, ArtifactKind],
    next_interrupt: Callable[[], str],
    *,
    run: str,
    instance: str,
    step: str,
) -> tuple[NewEvent, ...]:
    """The diff reviews ``saved`` raise (module doc, rule 2), one per replaced version."""
    out: list[NewEvent] = []
    for revision in saved:
        if revision.origin == "choice":  # the person's own answer, just given: no review
            continue
        old = current(view, revision.ref.kind, revision.ref.key)
        if old is None or old.ref.version >= revision.ref.version:
            continue
        state = iv.versions.get(str(old.ref))
        kind = kinds.get(revision.ref.kind)
        if state is None or kind is None or not state.human_reviewed or not state.stale:
            continue
        review = diff_review(old, state, revision, kind, next_interrupt(), run=run,
                             instance=instance)  # fmt: skip
        out.append(review_raised(review, step=step))
    return tuple(out)


def step_confirmations(
    view: LogView,
    events: Sequence[NewEvent],
    rows: Sequence[ArtifactRow],
    saved: Sequence[ArtifactVersion],
    kinds: Mapping[str, ArtifactKind],
    next_interrupt: Callable[[], str],
    *,
    run: str,
    instance: str,
    step: str,
) -> tuple[NewEvent, ...]:
    """The reviews and confirmations to append after ``events`` (module doc, rule 3)."""
    if not saved:
        return ()
    reviews = review_facts(view, invalidation_view(view), saved, kinds, next_interrupt,
                           run=run, instance=instance, step=step)  # fmt: skip
    return (*reviews, *confirmations(view, (*events, *reviews), rows))
