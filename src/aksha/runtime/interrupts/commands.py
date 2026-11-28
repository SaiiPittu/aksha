"""The commands that decide interrupts (ADR-P17 D4, D5, D8, S1 to S3): only a person resolves.

Each goes through the session API's one write path (``session.commands.dispatch``), so
its facts commit with ``command.accepted`` under its idempotency key (U4), and a retry
returns the first receipt. A decision on a closed interrupt or a decided approval is
refused ``already_decided``, and an approval's decision carries the dedup key
``decide:{approval}``, so two front ends racing to decide get one decision and one
``command.refused{already_decided}`` even when both read the log before either wrote.

1. :func:`answer` resolves an ``input``, ``choice`` or ``route`` interrupt with free text
   (IE-10). Approval, budget and review interrupts are refused
   ``explicit_command_required``: free text never approves. So is the request router's
   own ``route`` question (one that names its ``message``): ``answer_route`` in
   ``router/commands.py`` answers it, because the run opens in the answer's transaction
   (ADR-P23 D4). An ``input`` with several
   questions takes ``answers`` keyed by question ID (S2), and each answer of an
   instance-scoped ``input`` or ``choice`` is minted as its own statement span (ADR-P13),
   so what is built from it has the answer in its lineage. A choice's answer must be one
   of its options (``not_an_option``); a ``multiple`` choice takes any subset of them,
   ``none`` included, as ``choices`` or as text (``"C1, C4"``, or the word ``none``; an
   empty answer is refused ``answer_missing``). Every choice's resolution
   records ``chosen``, the codes in option order. A blocked
   instance is reset in the same transaction (``completion_check.reset{cause: answer}``).
2. :func:`approve` grants an approval for the signature the person was shown. Refusals:
   ``signature_mismatch`` (another signature), ``target_changing`` (a target is invalidated,
   dirty or pending review, S1), ``already_decided``, ``approval_not_found``.
3. :func:`reject` writes ``approval.rejected`` and nothing else changes.
4. :func:`edit` voids the approval (``approval.voided{replaced_by_edit}``, and its open
   interrupt) and requests a new one for the edited arguments, under a new signature.
5. :func:`grant_budget` answers a budget interrupt with ``budget.granted`` (ADR-P14).
   :func:`finalize` is its third answer (ADR-P24 D4): ``run.finalizing`` voids the work
   not yet settled and lets only finalizer Tasks run, on the reserved budget. Refused
   ``already_finalizing`` once the run is finalizing.
6. :func:`handoff` is the first milestone's execution path (D8): it re-verifies the
   approval from current facts and only then writes the outbox entry, whose
   ``approval_id`` marks the approval used. A refusal writes no entry.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.budgets.facts import budget_granted
from aksha.runtime.budgets.types import Dimension
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.ports import IdSource
from aksha.runtime.finalization.finalize import finalize_facts
from aksha.runtime.finalization.state import finalizing
from aksha.runtime.grounding.spans import span_index, statement_span
from aksha.runtime.grounding.types import SpanIndex
from aksha.runtime.interrupts.approvals import (
    APPROVAL_GRANTED,
    APPROVAL_REJECTED,
    APPROVAL_VOIDED,
    NOT_APPROVABLE,
    NOT_FOUND,
    SIGNATURE_MISMATCH,
    TARGET_CHANGING,
    ApprovalTool,
    approvals,
    build_body,
    request_events,
    verify_for_execution,
)
from aksha.runtime.interrupts.reducers import INTERRUPT_RESOLVED, INTERRUPT_VOIDED, interrupts
from aksha.runtime.interrupts.types import FREE_TEXT_KINDS, InterruptView, Refusal
from aksha.runtime.invalidation.effects import effect_refusal
from aksha.runtime.invalidation.view import invalidation_view
from aksha.runtime.outbox.errors import OutboxRefused
from aksha.runtime.outbox.outbox import enqueue
from aksha.runtime.projections.view import LogView
from aksha.runtime.session.commands import ALREADY_DECIDED, derive_command_id, dispatch
from aksha.runtime.session.types import (
    Command,
    CommandAccepted,
    CommandReceipt,
    CommandRefused,
    CommandSpec,
)
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.tasks.completion import reset_fact
from aksha.runtime.tasks.status import instance_status
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import InstanceRef

DECISION_BOUNDARY = "U4"
INTERRUPT_NOT_OPEN = "interrupt_not_open"
EXPLICIT_COMMAND_REQUIRED = "explicit_command_required"
NOT_AN_OPTION = "not_an_option"
ANSWER_MISSING = "answer_missing"
NOT_A_BUDGET_INTERRUPT = "not_a_budget_interrupt"
ALREADY_FINALIZING = "already_finalizing"


def _command(
    name: str, owner: str, args: dict[str, Any], command_id: str | None, session: str | None,
    answers: str,
) -> Command:  # fmt: skip
    command = Command(name, owner, session or owner, args, command_id, answers=answers)
    if command_id is None:
        command = Command(name, owner, command.session, args, derive_command_id(command),
                          answers=answers)  # fmt: skip
    return command


def _refused(r: Refusal) -> CommandRefused:
    return CommandRefused(r.code, r.message)


def _resolved(i: InterruptView, by: str, **fields: Any) -> NewEvent:
    """``interrupt.resolved`` in the interrupt's own scope, so every reader sees it closed."""
    payload = {"interrupt": i.id, "by": by, **fields}
    return NewEvent(INTERRUPT_RESOLVED, payload, run=i.run, instance=i.instance, session=i.session)


def _open(view: LogView, interrupt_id: str) -> InterruptView | CommandRefused:
    found = interrupts(view).get(interrupt_id)
    if found is None:
        return CommandRefused(INTERRUPT_NOT_OPEN, f"no interrupt {interrupt_id}")
    if not found.is_open:
        code = ALREADY_DECIDED if found.state == "resolved" else INTERRUPT_NOT_OPEN
        return CommandRefused(code, f"{interrupt_id} is {found.state}")
    return found


# --- answer ---------------------------------------------------------------------------


def _answers(i: InterruptView, args: Mapping[str, Any]) -> dict[str, str] | CommandRefused:
    """The answer per question ID, or a refusal (S2)."""
    questions = [str(q["id"]) for q in i.payload.get("questions", ())]
    given = args.get("answers")
    text = args.get("text")
    if given is None:
        if text is None or len(questions) > 1:
            return CommandRefused(ANSWER_MISSING, f"{i.id} asks {len(questions)} questions")
        return {questions[0] if questions else "q1": str(text)}
    out = {str(k): str(v) for k, v in dict(given).items()}
    missing = [q for q in questions if not out.get(q)]
    extra = sorted(set(out) - set(questions))
    if missing or extra:
        return CommandRefused(ANSWER_MISSING, f"answers missing {missing}, unknown {extra}")
    return out


NONE_CHOSEN = "none"
"""The answer that chooses none of a ``multiple`` choice's options."""


def _picked(
    i: InterruptView, args: Mapping[str, Any], options: tuple[str, ...]
) -> tuple[str, ...] | CommandRefused:
    """The codes a ``multiple`` choice's answer picks, in option order, or a refusal.

    ``choices`` lists them (an empty list picks none); otherwise ``text`` does, separated
    by commas or spaces, and only the word ``none`` picks none: an empty or blank answer
    is refused (``answer_missing``), so an accidental empty submit is never recorded as a
    choice of nothing. Every code must be an option (``not_an_option``).
    """
    listed = args.get("choices")
    if listed is None:
        text = args.get("text")
        if text is None:
            given = args.get("answers") or {}
            text = next(iter(dict(given).values()), None)
        if text is None:
            return CommandRefused(ANSWER_MISSING, f"{i.id} needs the chosen codes, or none")
        words = [w for w in str(text).replace(",", " ").split() if w]
        if not words:
            return CommandRefused(ANSWER_MISSING, f"{i.id} needs the chosen codes, or 'none'")
        listed = [] if [w.lower() for w in words] == [NONE_CHOSEN] else words
    codes = [str(c) for c in listed]
    unknown = [c for c in codes if c not in options]
    if unknown:
        return CommandRefused(NOT_AN_OPTION, f"{i.id} offers {list(options)}, not {unknown}")
    return tuple(o for o in options if o in codes)


def answer_spec(tree: CompiledTree | None = None) -> CommandSpec:
    """The ``answer`` command; ``tree`` lets it reset a blocked instance it answers."""

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        i = _open(view, str(command.args["interrupt"]))
        if isinstance(i, CommandRefused):
            return i
        if i.kind not in FREE_TEXT_KINDS:
            return CommandRefused(EXPLICIT_COMMAND_REQUIRED,
                                  f"a {i.kind} interrupt needs its own command")  # fmt: skip
        if i.kind == "route" and "message" in i.payload:
            # ADR-P23 D4: the router's question opens its run in the answer's transaction.
            return CommandRefused(EXPLICIT_COMMAND_REQUIRED,
                                  f"{i.id} is answered with answer_route")  # fmt: skip
        options = tuple(str(o) for o in i.payload.get("options", ()))
        fields: dict[str, Any] = {}
        if i.kind == "choice" and i.payload.get("multiple") is True:
            picked = _picked(i, command.args, options)
            if isinstance(picked, CommandRefused):
                return picked
            questions = [str(q["id"]) for q in i.payload.get("questions", ())]
            found: dict[str, str] = {(questions or ["q1"])[0]: ", ".join(picked) or NONE_CHOSEN}
            fields["chosen"] = list(picked)
        else:
            answered = _answers(i, command.args)
            if isinstance(answered, CommandRefused):
                return answered
            found = answered
            if i.kind in ("choice", "route") and any(a not in options for a in found.values()):
                return CommandRefused(NOT_AN_OPTION, f"{i.id} offers {list(options)}")
            if i.kind == "choice":
                fields["chosen"] = list(dict.fromkeys(found.values()))
        by = str(command.args["by"])
        events: list[NewEvent] = [_resolved(i, by, answers=found, **fields)]
        if i.run is not None and i.instance is not None and i.kind in ("input", "choice"):
            events.extend(_statements(span_index(view), i, found, str(command.command_id)))
        if tree is not None and i.run is not None and i.instance is not None:
            ref = InstanceRef.parse(i.run, i.instance)
            if instance_status(view, tree, ref) == "blocked":
                events.append(reset_fact(ref, "answer"))
        return CommandAccepted(tuple(events))

    return CommandSpec("answer", DECISION_BOUNDARY, decide)


def _statements(
    index: SpanIndex, i: InterruptView, answers: Mapping[str, str], command_id: str
) -> list[NewEvent]:
    """One statement span per answer (S2), each minted after the one before."""
    assert i.run is not None and i.instance is not None
    out: list[NewEvent] = []
    for qid, text in answers.items():
        minted = statement_span(index, text, interrupt=i.id, answer=f"{command_id}/{qid}",
                                run=i.run, instance=i.instance)  # fmt: skip
        out.extend(minted.events)
        index = SpanIndex(index.tenant, {**index.spans, **{s.span_id: s for s in minted.spans}})
    return out


def answer(
    store: EventStore,
    owner: str,
    interrupt_id: str,
    *,
    by: str,
    text: str | None = None,
    answers: Mapping[str, str] | None = None,
    tree: CompiledTree | None = None,
    command_id: str | None = None,
    session: str | None = None,
    choices: tuple[str, ...] | list[str] | None = None,
) -> CommandReceipt:
    """A person answers an ``input``, ``choice`` or ``route`` interrupt (module doc, item 1).

    ``choices`` lists the codes chosen on a ``multiple`` choice (an empty list chooses
    none); ``text`` may give them instead, as ``"C1, C4"`` or ``"none"``.
    """
    args: dict[str, Any] = {"interrupt": interrupt_id, "by": by}
    if text is not None:
        args["text"] = text
    if answers is not None:
        args["answers"] = dict(answers)
    if choices is not None:
        args["choices"] = [str(c) for c in choices]
    command = _command("answer", owner, args, command_id, session, interrupt_id)
    return dispatch(store, command, answer_spec(tree))


# --- approve, reject, edit ------------------------------------------------------------


def _decided(view: LogView, approval_id: str) -> CommandRefused | None:
    a = approvals(view).get(approval_id)
    if a is None:
        return CommandRefused(NOT_FOUND, f"no approval {approval_id}")
    if a.state != "requested":
        return CommandRefused(ALREADY_DECIDED, f"{approval_id} is {a.state}")
    return None


def _close_interrupt(view: LogView, approval_id: str, by: str, **fields: Any) -> list[NewEvent]:
    """The approval's own interrupt, resolved if it is still open."""
    i = interrupts(view).get(approval_id)
    return [_resolved(i, by, **fields)] if i is not None and i.is_open else []


def approve_spec() -> CommandSpec:
    """The ``approve`` command: a grant for exactly the shown signature (D5, S1)."""

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        approval_id = str(command.args["approval"])
        refused = _decided(view, approval_id)
        if refused is not None:
            return refused
        a = approvals(view)[approval_id]
        if str(command.args["signature"]) != a.signature:
            return CommandRefused(SIGNATURE_MISMATCH, f"{approval_id} is not the shown action")
        changing = effect_refusal(invalidation_view(view), a.body.target_refs())
        if changing is not None:
            return CommandRefused(TARGET_CHANGING, f"a target of {approval_id} is {changing}")
        by = str(command.args["by"])
        payload = {"approval": approval_id, "signature": a.signature, "by": by,
                   "targets": list(a.body.target_refs())}  # fmt: skip
        granted = NewEvent(APPROVAL_GRANTED, payload, run=a.run, instance=a.instance)
        closing = _close_interrupt(view, approval_id, by, decision="granted")
        return CommandAccepted((granted, *closing))

    return CommandSpec("approve", DECISION_BOUNDARY, decide)


def reject_spec() -> CommandSpec:
    """The ``reject`` command: ``approval.rejected``; nothing else changes (D5)."""

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        approval_id = str(command.args["approval"])
        refused = _decided(view, approval_id)
        if refused is not None:
            return refused
        a = approvals(view)[approval_id]
        by = str(command.args["by"])
        payload: dict[str, Any] = {"approval": approval_id, "signature": a.signature, "by": by}
        if command.args.get("note"):
            payload["note"] = str(command.args["note"])
        rejected = NewEvent(APPROVAL_REJECTED, payload, run=a.run, instance=a.instance)
        closing = _close_interrupt(view, approval_id, by, decision="rejected")
        return CommandAccepted((rejected, *closing))

    return CommandSpec("reject", DECISION_BOUNDARY, decide)


def edit_spec(tools: Mapping[str, ApprovalTool], ids: IdSource) -> CommandSpec:
    """The ``edit`` command: void the approval, request a new one for the edited call."""

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        approval_id = str(command.args["approval"])
        a = approvals(view).get(approval_id)
        if a is None:
            return CommandRefused(NOT_FOUND, f"no approval {approval_id}")
        if a.state not in ("requested", "granted"):
            return CommandRefused(ALREADY_DECIDED, f"{approval_id} is {a.state}")
        tool = tools.get(a.body.tool)
        if tool is None:
            return CommandRefused(NOT_APPROVABLE, f"no approval tool {a.body.tool}")
        args = {**a.body.args(), **dict(command.args["changes"])}
        body = build_body(view, tool, args, definition_hash=a.body.definition_hash)
        if isinstance(body, Refusal):
            return _refused(body)
        voided = NewEvent(APPROVAL_VOIDED, {"approval": approval_id, "reason": "replaced_by_edit"},
                          run=a.run, instance=a.instance)  # fmt: skip
        events: list[NewEvent] = [voided]
        i = interrupts(view).get(approval_id)
        if i is not None and i.is_open:
            payload = {"interrupt": i.id, "reason": "replaced_by_edit"}
            events.append(NewEvent(INTERRUPT_VOIDED, payload, run=i.run, instance=i.instance))
        fresh = ids.next(view.owner, "A")
        events.extend(request_events(fresh, body, run=a.run, instance=a.instance,
                                     replaces=approval_id))  # fmt: skip
        return CommandAccepted(tuple(events))

    return CommandSpec("edit", DECISION_BOUNDARY, decide)


def approve(
    store: EventStore,
    owner: str,
    approval_id: str,
    shown_signature: str,
    *,
    by: str,
    command_id: str | None = None,
    session: str | None = None,
) -> CommandReceipt:
    """A person approves ``approval_id`` for the signature they were shown (item 2)."""
    args: dict[str, Any] = {"approval": approval_id, "signature": shown_signature, "by": by}
    command = _command("approve", owner, args, command_id, session, approval_id)
    return dispatch(store, command, approve_spec())


def reject(
    store: EventStore,
    owner: str,
    approval_id: str,
    *,
    by: str,
    note: str | None = None,
    command_id: str | None = None,
    session: str | None = None,
) -> CommandReceipt:
    """A person rejects ``approval_id`` (item 3)."""
    args: dict[str, Any] = {"approval": approval_id, "by": by}
    if note:
        args["note"] = note
    command = _command("reject", owner, args, command_id, session, approval_id)
    return dispatch(store, command, reject_spec())


def edit(
    store: EventStore,
    owner: str,
    approval_id: str,
    changes: Mapping[str, Any],
    *,
    tools: Mapping[str, ApprovalTool],
    ids: IdSource,
    command_id: str | None = None,
    session: str | None = None,
) -> CommandReceipt:
    """A person edits the action ``approval_id`` covers (item 4)."""
    args: dict[str, Any] = {"approval": approval_id, "changes": dict(changes)}
    command = _command("edit", owner, args, command_id, session, approval_id)
    return dispatch(store, command, edit_spec(tools, ids))


# --- grant_budget ---------------------------------------------------------------------


def grant_budget_spec() -> CommandSpec:
    """The ``grant_budget`` command: ``budget.granted`` answers the budget interrupt."""

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        i = _open(view, str(command.args["interrupt"]))
        if isinstance(i, CommandRefused):
            return i
        if i.kind != "budget" or i.run is None:
            return CommandRefused(NOT_A_BUDGET_INTERRUPT, f"{i.id} is a {i.kind} interrupt")
        by = str(command.args["by"])
        extra: dict[Dimension, int] = dict(command.args["extra"])
        try:
            granted = budget_granted(i.run, i.id, by, extra)
        except ValueError as e:
            return CommandRefused("grant_invalid", str(e))
        return CommandAccepted((granted, _resolved(i, by, decision="granted")))

    return CommandSpec("grant_budget", DECISION_BOUNDARY, decide)


def grant_budget(
    store: EventStore,
    owner: str,
    interrupt_id: str,
    extra: Mapping[Dimension, int],
    *,
    by: str,
    command_id: str | None = None,
    session: str | None = None,
) -> CommandReceipt:
    """A person grants more budget, answering a budget interrupt (item 5)."""
    args: dict[str, Any] = {"interrupt": interrupt_id, "extra": dict(extra), "by": by}
    command = _command("grant_budget", owner, args, command_id, session, interrupt_id)
    return dispatch(store, command, grant_budget_spec())


def finalize_spec(tree: CompiledTree) -> CommandSpec:
    """The ``finalize`` command: ``run.finalizing`` answers the budget interrupt (ADR-P24)."""

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        i = _open(view, str(command.args["interrupt"]))
        if isinstance(i, CommandRefused):
            return i
        if i.kind != "budget" or i.run is None:
            return CommandRefused(NOT_A_BUDGET_INTERRUPT, f"{i.id} is a {i.kind} interrupt")
        if finalizing(view, i.run) is not None:
            return CommandRefused(ALREADY_FINALIZING, f"run {i.run} is already finalizing")
        by = str(command.args["by"])
        facts = finalize_facts(view, tree, i.run, i.id, by)
        return CommandAccepted((*facts, _resolved(i, by, decision="finalized")))

    return CommandSpec("finalize", DECISION_BOUNDARY, decide)


def finalize(
    store: EventStore,
    owner: str,
    interrupt_id: str,
    *,
    tree: CompiledTree,
    by: str,
    command_id: str | None = None,
    session: str | None = None,
) -> CommandReceipt:
    """A person answers a budget interrupt with "finalize" (ADR-P24 D4)."""
    args: dict[str, Any] = {"interrupt": interrupt_id, "by": by}
    command = _command("finalize", owner, args, command_id, session, interrupt_id)
    return dispatch(store, command, finalize_spec(tree))


# --- handoff --------------------------------------------------------------------------


def handoff_spec(tools: Mapping[str, ApprovalTool], definition_hash: str) -> CommandSpec:
    """The ``handoff`` command: re-verify, then write the outbox entry (D8)."""

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        approval_id = str(command.args["approval"])
        a = approvals(view).get(approval_id)
        if a is None:
            return CommandRefused(NOT_FOUND, f"no approval {approval_id}")
        tool = tools.get(a.body.tool)
        if tool is None:
            return CommandRefused(NOT_APPROVABLE, f"no approval tool {a.body.tool}")
        body = verify_for_execution(view, approval_id, tool, definition_hash=definition_hash)
        if isinstance(body, Refusal):
            return _refused(body)
        effect = tool.spec.external
        assert effect is not None  # checked by ApprovalTool
        try:
            out = enqueue(view, body.tool, body.args(), effect, approval_id=approval_id,
                          command_id=command.command_id, session=command.session,
                          run=a.run, resolve=tool.resolve)  # fmt: skip
        except OutboxRefused as e:
            return CommandRefused(e.code, e.message)
        if out.event is None:
            return CommandRefused(ALREADY_DECIDED, f"{body.idempotency_key} is already enqueued")
        return CommandAccepted((out.event,))

    return CommandSpec("handoff", DECISION_BOUNDARY, decide)


def handoff(
    store: EventStore,
    owner: str,
    approval_id: str,
    *,
    tools: Mapping[str, ApprovalTool],
    definition_hash: str,
    command_id: str | None = None,
    session: str | None = None,
) -> CommandReceipt:
    """Execute an approved action through the outbox (item 6)."""
    args: dict[str, Any] = {"approval": approval_id}
    command = _command("handoff", owner, args, command_id, session, approval_id)
    return dispatch(store, command, handoff_spec(tools, definition_hash))
