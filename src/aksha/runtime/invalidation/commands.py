"""The invalidation commands and recovery's source check (ADR-P05 D1, D6, S2, S4).

Each command goes through the session API's one write path (``session.commands.dispatch``),
so its facts commit with ``command.accepted`` under the command's idempotency key, and a
retry returns the first receipt.

1. ``apply_source_change`` (U8) records what moved for one owner through
   :func:`~aksha.runtime.invalidation.sources.record_source_changes`, with the revocations
   and review voids that change confirms (D9), in one transaction. The caller has already
   applied the change set to its source adapter (a fixture world's per-owner overlay); the
   command reads the adapters and records the facts. Nothing moved: the receipt alone.
   Citation-keyed versions a moved read did not reopen are noticed in the same transaction
   (``evidence.noticed``, :mod:`invalidation.notices`, amendment M2).
2. ``accept_review`` (U4) records ``review.accepted`` and ``interrupt.resolved``. Where
   invalidation revoked an approval of the old version, it records a fresh
   ``approval.requested`` (with its ``interrupt.raised{approval}``) for the revision,
   replacing it: the revoked body with that target moved to the revision and its content
   hash, so a new signature (ADR-P17 D7). It never records ``approval.granted``, which only
   the person's separate approve does. Refusals: ``review_not_open`` (never raised, or
   already closed: decided, or voided because the revision went stale),
   ``card_hash_mismatch`` (the person saw another card), ``revision_invalidated``.
3. ``reject_review`` (U4) records ``review.rejected`` and ``interrupt.resolved``; the old
   version stays invalidated and nothing on it is sendable, and the plan skips the writer
   (``review_rejected``, S4). For a version a person chose, the same transaction raises a
   fresh ``choice`` interrupt on the writer, so the person can choose again.

:func:`check_sources` is recovery's source check (S2): the same one writer, cause
``recovery``, committed in U8 without a command receipt.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from aksha.runtime.artifacts.versions import version
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.ports import IdSource
from aksha.runtime.interrupts.approvals import ApprovalTool, request_events, retarget
from aksha.runtime.interrupts.approvals import approvals as approval_views
from aksha.runtime.invalidation.confirm import after, confirmations
from aksha.runtime.invalidation.notices import notices_for
from aksha.runtime.invalidation.ports import QueryRunner, VersionedSource
from aksha.runtime.invalidation.reviews import ReviewRecord, approvals, reviews
from aksha.runtime.invalidation.sources import SOURCE_CHANGE_BOUNDARY, record_source_changes
from aksha.runtime.invalidation.view import HUMAN_ORIGINS, invalidation_view
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.session.commands import derive_command_id, dispatch
from aksha.runtime.session.types import (
    Command,
    CommandAccepted,
    CommandReceipt,
    CommandRefused,
    CommandSpec,
)
from aksha.runtime.storage.ports import EventStore

DECISION_BOUNDARY = "U4"
REVIEW_NOT_OPEN = "review_not_open"
CARD_HASH_MISMATCH = "card_hash_mismatch"
REVISION_INVALIDATED = "revision_invalidated"
REVIEW_REJECTED = "review_rejected"


def _command(
    name: str,
    owner: str,
    args: dict[str, object],
    command_id: str | None,
    session: str | None,
    answers: str | None = None,
) -> Command:
    command = Command(name, owner, session or owner, args, command_id, answers=answers)
    if command_id is None:
        command = Command(
            name, owner, command.session, args, derive_command_id(command), answers=answers
        )
    return command


# --- apply_source_change -----------------------------------------------------------------


def source_change_spec(
    sources: Iterable[VersionedSource],
    run_query: QueryRunner | None = None,
    facts: Sequence[NewEvent] = (),
) -> CommandSpec:
    """The ``apply_source_change`` command over these source adapters.

    ``facts`` commit first in the same transaction: an upload's ``document.*`` facts.
    """
    held = tuple(sources)

    def decide(command: Command, view: LogView) -> CommandAccepted:
        change_set = command.args.get("change_set")
        events = record_source_changes(
            view, held, cause="command", run_query=run_query,
            change_set=None if change_set is None else str(change_set),
        )  # fmt: skip
        if not events:
            return CommandAccepted(tuple(facts))
        return CommandAccepted(
            (*facts, *events, *confirmations(view, events), *notices_for(view, events))
        )

    return CommandSpec("apply_source_change", SOURCE_CHANGE_BOUNDARY, decide)


def apply_source_change(
    store: EventStore,
    owner: str,
    sources: Iterable[VersionedSource],
    change_set: str,
    *,
    run_query: QueryRunner | None = None,
    command_id: str | None = None,
    session: str | None = None,
    facts: Sequence[NewEvent] = (),
) -> CommandReceipt:
    """Record what ``change_set`` moved for ``owner`` (module doc, item 1), after ``facts``."""
    command = _command("apply_source_change", owner, {"change_set": change_set}, command_id,
                       session)  # fmt: skip
    return dispatch(store, command, source_change_spec(sources, run_query, facts))


def check_sources(
    store: EventStore,
    owner: str,
    sources: Iterable[VersionedSource],
    *,
    run_query: QueryRunner | None = None,
) -> tuple[str, ...]:
    """Recovery's source check (S2): record what moved; return the sources that moved."""
    view = load_view(store, owner)
    events = record_source_changes(view, sources, cause="recovery", run_query=run_query)
    if not events:
        return ()
    store.append(owner, SOURCE_CHANGE_BOUNDARY,
                 (*events, *confirmations(view, events), *notices_for(view, events)))  # fmt: skip
    return tuple(sorted(events[0].payload["sources"]))


# --- accept_review and reject_review ---------------------------------------------------


def _open_review(view: LogView, interrupt: str, card: str) -> ReviewRecord | CommandRefused:
    found = [r for r in reviews(view) if r.interrupt == interrupt]
    if not found or found[0].outcome != "open":
        return CommandRefused(REVIEW_NOT_OPEN, f"no open diff review {interrupt}")
    review = found[0]
    if review.card_hash != card:
        return CommandRefused(CARD_HASH_MISMATCH, f"{interrupt} shows another card")
    state = invalidation_view(view).versions.get(str(review.revision))
    if state is not None and state.stale:
        return CommandRefused(REVISION_INVALIDATED, f"{review.revision} is {state.freshness}")
    return review


def _decision(type_: str, review: ReviewRecord) -> tuple[NewEvent, ...]:
    payload = {"interrupt": review.interrupt, "old": str(review.old),
               "revision": str(review.revision), "card_hash": review.card_hash}  # fmt: skip
    resolved = NewEvent("interrupt.resolved", {"interrupt": review.interrupt}, run=review.run)
    return (NewEvent(type_, payload, run=review.run), resolved)


def accept_spec(ids: IdSource, tools: Mapping[str, ApprovalTool] | None = None) -> CommandSpec:
    """The ``accept_review`` command; new approval request IDs come from ``ids``.

    ``tools`` (by tool name) lets the fresh request rebuild the moved call's body, so the
    approval it asks for is one execution can carry out (``retarget``).
    """

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        review = _open_review(view, str(command.args["interrupt"]),
                              str(command.args["card_hash"]))  # fmt: skip
        if isinstance(review, CommandRefused):
            return review
        events = list(_decision("review.accepted", review))
        accepted = after(view, events)  # the fresh body's facts read the accepted revision
        old = str(review.old)
        bodies = approval_views(view)
        revision = version(view, review.revision)
        for a in approvals(view):
            held = bodies.get(a.approval)
            if not a.revoked or old not in a.targets or held is None or revision is None:
                continue
            tool = (tools or {}).get(held.body.tool)
            body = retarget(held.body, old, str(review.revision), revision.content_hash,
                            view=accepted, tool=tool)  # fmt: skip
            events.extend(request_events(ids.next(view.owner, "A"), body, run=held.run,
                                         instance=held.instance, replaces=a.approval))  # fmt: skip
        return CommandAccepted((*events, *confirmations(view, events)))

    return CommandSpec("accept_review", DECISION_BOUNDARY, decide)


def reject_spec(ids: IdSource) -> CommandSpec:
    """The ``reject_review`` command; the fresh choice interrupt's ID comes from ``ids``."""

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        review = _open_review(view, str(command.args["interrupt"]),
                              str(command.args["card_hash"]))  # fmt: skip
        if isinstance(review, CommandRefused):
            return review
        events = list(_decision("review.rejected", review))
        old = version(view, review.old)
        if old is not None and old.origin in HUMAN_ORIGINS:
            payload = {"interrupt": ids.next(view.owner, "I"), "kind": "choice",
                       "reason": REVIEW_REJECTED, "old": str(review.old),
                       "writer": old.writer.as_json()}  # fmt: skip
            events.append(
                NewEvent("interrupt.raised", payload, run=review.run, instance=review.instance)
            )
        return CommandAccepted(tuple(events))

    return CommandSpec("reject_review", DECISION_BOUNDARY, decide)


def accept_review(
    store: EventStore,
    owner: str,
    interrupt_id: str,
    card_hash: str,
    ids: IdSource,
    *,
    command_id: str | None = None,
    session: str | None = None,
    tools: Mapping[str, ApprovalTool] | None = None,
) -> CommandReceipt:
    """A person accepts a diff review (module doc, item 2); ``tools`` as in ``accept_spec``."""
    args: dict[str, object] = {"interrupt": interrupt_id, "card_hash": card_hash}
    command = _command("accept_review", owner, args, command_id, session, interrupt_id)
    return dispatch(store, command, accept_spec(ids, tools))


def reject_review(
    store: EventStore,
    owner: str,
    interrupt_id: str,
    card_hash: str,
    ids: IdSource,
    *,
    command_id: str | None = None,
    session: str | None = None,
) -> CommandReceipt:
    """A person rejects a diff review (module doc, item 3)."""
    args: dict[str, object] = {"interrupt": interrupt_id, "card_hash": card_hash}
    command = _command("reject_review", owner, args, command_id, session, interrupt_id)
    return dispatch(store, command, reject_spec(ids))
