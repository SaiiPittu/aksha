"""The router's commands through the session API (ADR-P23 D4, D5, D7; ADR-P19 S2, S3).

1. :func:`send_message` (``SendMessage``) commits ``user.message`` with its receipt (U1).
   Refused ``lane_required`` when the session has an open run (D7: routing applies to new
   requests only), ``route_pending`` while a route question of the session is open (the
   intake is held, ADR-P16 D6), and ``agent_unknown`` for an agent no one loaded. A
   message that names its agent skips the router (D5): ``route.decided{by: person}`` and
   ``run.started`` commit with it, and no classification step is written.
2. :func:`answer_route` (``AnswerInterrupt(agent=...)``) answers a route question with one
   of its listed agents: ``interrupt.resolved``, ``route.decided{by: person}`` and
   ``run.started`` in the one answer transaction (U4, D4). Refusals: ``not_an_option``,
   ``already_decided``, ``interrupt_not_open``, ``not_a_route``.
3. :func:`submit` is a message and its route: :func:`send_message`, then
   ``router.route_message`` when no agent was named.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.interrupts.commands import INTERRUPT_NOT_OPEN, NOT_AN_OPTION
from aksha.runtime.interrupts.reducers import (
    INTERRUPT_RESOLVED,
    interrupts,
    open_session_interrupts,
)
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.router.decisions import route_decisions, route_step_id, session_open_runs
from aksha.runtime.router.facts import USER_MESSAGE, opening, user_message
from aksha.runtime.router.router import Router, route_message
from aksha.runtime.router.types import RouteOutcome
from aksha.runtime.session.commands import ALREADY_DECIDED, derive_command_id, dispatch
from aksha.runtime.session.types import (
    Command,
    CommandAccepted,
    CommandReceipt,
    CommandRefused,
    CommandSpec,
)
from aksha.runtime.storage.ports import EventStore

START = "U1"
RESOLUTION = "U4"
LANE_REQUIRED = "lane_required"
ROUTE_PENDING = "route_pending"
AGENT_UNKNOWN = "agent_unknown"
NOT_A_ROUTE = "not_a_route"


@dataclass(frozen=True)
class MessageSent:
    """A message's receipt and the message ID it was recorded under (None when refused)."""

    receipt: CommandReceipt
    message: str | None


def _intake_refusal(view: LogView, session: str) -> CommandRefused | None:
    """D7 and the session hold: why a new request may not enter ``session`` now."""
    running = session_open_runs(view, session)
    if running:
        return CommandRefused(LANE_REQUIRED, f"{session} has the open run {running[0]}")
    if open_session_interrupts(view, session):
        return CommandRefused(ROUTE_PENDING, f"{session} waits for the answer to a route question")
    return None


def send_message_spec(router: Router) -> CommandSpec:
    """The ``send_message`` command (module doc, item 1)."""

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        session = command.session
        refused = _intake_refusal(view, session)
        if refused is not None:
            return refused
        named = command.args.get("agent")
        agent = None if named is None else router.agent(str(named))
        if named is not None and agent is None:
            return CommandRefused(AGENT_UNKNOWN, f"no loaded agent {named!r}")
        message = router.ids.next(view.owner, "M")
        text = str(command.args["text"])
        events: list[NewEvent] = [user_message(session, message, text, None if agent is None
                                               else agent.name)]  # fmt: skip
        if agent is not None:
            run = router.ids.next(view.owner, "R")
            events.extend(opening(session, message, agent, "person", run))
        return CommandAccepted(tuple(events))

    return CommandSpec("send_message", START, decide)


def _command(
    name: str, owner: str, session: str, args: dict[str, Any], command_id: str | None,
    answers: str | None = None,
) -> Command:  # fmt: skip
    command = Command(name, owner, session, args, command_id, answers=answers)
    if command_id is None:
        command = Command(name, owner, session, args, derive_command_id(command), answers=answers)
    return command


def send_message(
    store: EventStore,
    owner: str,
    router: Router,
    session: str,
    text: str,
    *,
    agent: str | None = None,
    command_id: str | None = None,
) -> MessageSent:
    """A person sends ``text`` to ``session``, naming ``agent`` or not (module doc, item 1)."""
    args: dict[str, Any] = {"text": text}
    if agent is not None:
        args["agent"] = agent
    receipt = dispatch(store, _command("send_message", owner, session, args, command_id),
                       send_message_spec(router))  # fmt: skip
    if not receipt.accepted:
        return MessageSent(receipt, None)
    sent = next(store.read(owner, after_seq=receipt.seq, upto_seq=receipt.seq + 1), None)
    if sent is None or sent.type != USER_MESSAGE:
        raise AssertionError(f"command {receipt.command_id} committed no message after its receipt")
    return MessageSent(receipt, str(sent.payload["message"]))


def answer_route_spec(router: Router) -> CommandSpec:
    """The ``answer_route`` command (module doc, item 2)."""

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        ident = str(command.args["interrupt"])
        i = interrupts(view).get(ident)
        if i is None:
            return CommandRefused(INTERRUPT_NOT_OPEN, f"no interrupt {ident}")
        message = i.payload.get("message")
        if i.kind != "route" or i.session is None or message is None:
            return CommandRefused(NOT_A_ROUTE, f"{ident} is not a route question")
        if not i.is_open:
            code = ALREADY_DECIDED if i.state == "resolved" else INTERRUPT_NOT_OPEN
            return CommandRefused(code, f"{ident} is {i.state}")
        name = str(command.args["agent"])
        agent = router.agent(name)
        if agent is None or name not in i.payload.get("options", ()):
            return CommandRefused(NOT_AN_OPTION, f"{ident} offers {list(i.payload['options'])}")
        if str(message) in route_decisions(view):
            return CommandRefused(ALREADY_DECIDED, f"{message} is already routed")
        by = str(command.args["by"])
        questions = [str(q["id"]) for q in i.payload.get("questions", ())] or ["q1"]
        resolved = NewEvent(INTERRUPT_RESOLVED, {"interrupt": ident, "by": by,
                                                 "answers": {questions[0]: name}},
                            session=i.session)  # fmt: skip
        step = route_step_id(i.session, str(message))
        run = router.ids.next(view.owner, "R")
        events = opening(i.session, str(message), agent, "person", run, step=step)
        return CommandAccepted((resolved, *events))

    return CommandSpec("answer_route", RESOLUTION, decide)


def answer_route(
    store: EventStore,
    owner: str,
    router: Router,
    interrupt_id: str,
    agent: str,
    *,
    by: str,
    command_id: str | None = None,
) -> CommandReceipt:
    """A person answers the route question ``interrupt_id`` with ``agent`` (item 2)."""
    session = _interrupt_session(store, owner, interrupt_id)
    args: dict[str, Any] = {"interrupt": interrupt_id, "agent": agent, "by": by}
    command = _command("answer_route", owner, session, args, command_id, answers=interrupt_id)
    return dispatch(store, command, answer_route_spec(router))


def _interrupt_session(store: EventStore, owner: str, interrupt_id: str) -> str:
    """The session an interrupt was raised in, or the owner when it names none."""
    for e in store.read(owner, types=frozenset({"interrupt.raised"})):
        if e.payload.get("interrupt") == interrupt_id and e.session is not None:
            return e.session
    return owner


def submit(
    store: EventStore,
    owner: str,
    router: Router,
    session: str,
    text: str,
    *,
    agent: str | None = None,
    command_id: str | None = None,
) -> tuple[MessageSent, RouteOutcome | None]:
    """A message and its route (item 3); the outcome is None when the message was refused."""
    sent = send_message(store, owner, router, session, text, agent=agent, command_id=command_id)
    if sent.message is None:
        return sent, None
    decided = route_decisions(load_view(store, owner)).get(sent.message)
    if decided is not None:
        return sent, decided
    return sent, route_message(store, owner, router, session, sent.message)
