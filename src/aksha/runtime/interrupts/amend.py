"""Amending an answered question (ADR-P27 D5, ADR-P17 S2): the new statement supersedes the old.

A person may correct the answer they gave to an ``input`` interrupt of a Task instance
after the run moved on. The amendment commits, in the command's transaction (U4):

1. one statement span per amended question, minted like the first answer's
   (:func:`~aksha.runtime.grounding.spans.statement_span`), whose ``supersedes`` names the
   span it corrects. A cite of the old span is then ``superseded_statement``, and the
   invalidation view reads every lineage input that names the old answer as ``moved``,
   so what was built from it, and what was built from that, rebuilds through the
   ordinary lineage closure (ADR-P05).
2. ``interrupt.amended{interrupt, by, answers}``, which the instance's context renders
   after the answer it corrects.

Two ways in, one path: ``AnswerInterrupt(amend=True)`` names the interrupt and its
answers by question ID; ``Steer(lane="amend")`` names the run, and its text amends the
run's last answered ``input`` interrupt (:func:`last_answered`), when that interrupt asks
one question. Refusals: ``interrupt_not_open`` (no such interrupt), ``not_answered``
(still open, or voided), ``not_amendable`` (not an instance's ``input`` interrupt),
``run_ended`` (its run has ended, so nothing would rebuild; a new message starts a new
run, ADR-P27 B8), ``answer_missing`` (an empty or unknown question ID).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.grounding.spans import span_index, statement_span
from aksha.runtime.grounding.types import STATEMENT_SOURCE, CitationSpan, SpanIndex
from aksha.runtime.inputs.types import RUN_ENDED
from aksha.runtime.interrupts.reducers import interrupts
from aksha.runtime.interrupts.types import InterruptView
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS
from aksha.runtime.projections.view import LogView
from aksha.runtime.session.commands import derive_command_id, dispatch
from aksha.runtime.session.types import (
    Command,
    CommandAccepted,
    CommandReceipt,
    CommandRefused,
    CommandSpec,
)
from aksha.runtime.storage.ports import EventStore

AMEND_BOUNDARY = "U4"
INTERRUPT_AMENDED = "interrupt.amended"
INTERRUPT_NOT_OPEN = "interrupt_not_open"
NOT_ANSWERED = "not_answered"
NOT_AMENDABLE = "not_amendable"
ANSWER_MISSING = "answer_missing"


def questions(i: InterruptView) -> tuple[str, ...]:
    """The interrupt's question IDs, in the order it asked them (``q1`` when it names none)."""
    found = tuple(str(q["id"]) for q in i.payload.get("questions", ()))
    return found or ("q1",)


def last_answered(view: LogView, run: str, instance: str | None = None) -> InterruptView | None:
    """The run's ``input`` interrupt a person answered or amended last (of ``instance``)."""
    found = interrupts(view)
    last: InterruptView | None = None
    for e in view.events:
        if e.run != run or e.type not in ("interrupt.resolved", INTERRUPT_AMENDED):
            continue
        i = found.get(str(e.payload.get("interrupt")))
        if i is None or i.kind != "input" or i.instance is None or i.state != "resolved":
            continue
        if instance is None or i.instance == instance:
            last = i
    return last


def amendment_refusal(i: InterruptView | None, ident: str) -> CommandRefused | None:
    """Why ``i`` cannot be amended, or None."""
    if i is None:
        return CommandRefused(INTERRUPT_NOT_OPEN, f"no interrupt {ident}")
    if i.kind != "input" or i.run is None or i.instance is None:
        return CommandRefused(NOT_AMENDABLE, f"{ident} is a {i.kind} interrupt of no instance")
    if i.state != "resolved":
        return CommandRefused(NOT_ANSWERED, f"{ident} is {i.state}, not answered")
    return None


def _current(index: SpanIndex, interrupt: str, qid: str) -> CitationSpan | None:
    """The statement span that stands for the answer to ``qid`` now: none supersedes it."""
    replaced = index.superseded()
    best: CitationSpan | None = None
    for s in index.spans.values():
        if s.source != STATEMENT_SOURCE or s.record_id != interrupt or s.span_id in replaced:
            continue
        if s.version.rsplit("/", 1)[-1] == qid and (best is None or _n(s) > _n(best)):
            best = s
    return best


def _n(span: CitationSpan) -> int:
    return int(span.span_id.split("-", 1)[1])


def amendment_facts(
    view: LogView,
    i: InterruptView,
    answers: Mapping[str, str],
    *,
    by: str,
    command_id: str,
    input_id: str | None = None,
) -> tuple[NewEvent, ...] | CommandRefused:
    """The statement spans and ``interrupt.amended`` of one amendment (module doc)."""
    asked = questions(i)
    given = {str(k): str(v) for k, v in answers.items()}
    if not given or any(not v for v in given.values()) or set(given) - set(asked):
        return CommandRefused(ANSWER_MISSING, f"{i.id} asks {list(asked)}; amend some of them")
    assert i.run is not None and i.instance is not None
    index = span_index(view)
    out: list[NewEvent] = []
    for qid in (q for q in asked if q in given):
        old = _current(index, i.id, qid)
        minted = statement_span(index, given[qid], interrupt=i.id, answer=f"{command_id}/{qid}",
                                run=i.run, instance=i.instance,
                                supersedes=None if old is None else old.span_id)  # fmt: skip
        out.extend(minted.events)
        index = SpanIndex(index.tenant, {**index.spans, **{s.span_id: s for s in minted.spans}})
    payload: dict[str, Any] = {"interrupt": i.id, "by": by,
                               "answers": {q: given[q] for q in asked if q in given}}  # fmt: skip
    if input_id is not None:
        payload["input_id"] = input_id
    out.append(NewEvent(INTERRUPT_AMENDED, payload, run=i.run, instance=i.instance,
                        session=i.session))  # fmt: skip
    return tuple(out)


def amend_spec() -> CommandSpec:
    """The ``amend_answer`` command: ``AnswerInterrupt(amend=True)``."""

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        ident = str(command.args["interrupt"])
        i = interrupts(view).get(ident)
        refused = amendment_refusal(i, ident)
        if refused is not None:
            return refused
        assert i is not None and i.run is not None
        summary = rebuild(RUNS, view).get(i.run)
        if summary is not None and summary.outcome is not None:
            return CommandRefused(RUN_ENDED, f"{i.run} ended {summary.outcome}; send a new message")
        given = command.args.get("answers")
        if not given:
            text = command.args.get("text")
            asked = questions(i)
            if text is None or len(asked) > 1:
                return CommandRefused(ANSWER_MISSING, f"{ident} asks {list(asked)}")
            given = {asked[0]: str(text)}
        facts = amendment_facts(view, i, dict(given), by=str(command.args["by"]),
                                command_id=str(command.command_id))  # fmt: skip
        if isinstance(facts, CommandRefused):
            return facts
        return CommandAccepted(facts)

    return CommandSpec("amend_answer", AMEND_BOUNDARY, decide)


def amend(
    store: EventStore,
    owner: str,
    interrupt_id: str,
    *,
    by: str,
    text: str | None = None,
    answers: Mapping[str, str] | None = None,
    command_id: str | None = None,
    session: str | None = None,
) -> CommandReceipt:
    """A person amends their answer to ``interrupt_id`` (module doc)."""
    args: dict[str, Any] = {"interrupt": interrupt_id, "by": by}
    if text is not None:
        args["text"] = text
    if answers:
        args["answers"] = dict(answers)
    command = Command("amend_answer", owner, session or owner, args, command_id,
                      answers=interrupt_id)  # fmt: skip
    if command_id is None:
        command = Command("amend_answer", owner, command.session, args,
                          derive_command_id(command), answers=interrupt_id)  # fmt: skip
    return dispatch(store, command, amend_spec())
