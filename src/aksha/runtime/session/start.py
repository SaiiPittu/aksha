"""Starting a run from a message that names its agent (ADR-P19 D1, ADR-P11 D12, ADR-P23 D5).

The command is ``send_message``, the router's own name, so a retry derives the same
command ID either way. Its decision, in order:

1. The session's intake: ``lane_required`` while the session has an open run (ADR-P23
   D7), ``route_pending`` while a route question of the session is open.
2. ``agent_unknown`` for an agent no one loaded.
3. ``sandbox_required`` for a headless start with no available sandbox backend
   (:func:`aksha.runtime.policy.headless.headless_start_refusal`), before anything is
   written.
4. Otherwise ``user.message``, ``route.decided{by: person}`` and ``run.started`` commit
   with the receipt. ``run.started`` records the definition hash, the extension API
   version, the agent's budget, the model binding (ADR-P07 D9) and, for a headless start,
   ``headless: true``, fixed for the run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from aksha.runtime.definitions.versions import API_VERSION
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.ports import IdSource
from aksha.runtime.interrupts.reducers import open_session_interrupts
from aksha.runtime.policy.headless import headless_start_refusal
from aksha.runtime.projections.view import LogView
from aksha.runtime.router.decisions import session_open_runs
from aksha.runtime.router.facts import opening, user_message
from aksha.runtime.router.types import RoutableAgent
from aksha.runtime.sandbox.types import Availability
from aksha.runtime.session.types import Command, CommandAccepted, CommandRefused, CommandSpec

START_BOUNDARY = "U1"
LANE_REQUIRED = "lane_required"
ROUTE_PENDING = "route_pending"
AGENT_UNKNOWN = "agent_unknown"


@dataclass(frozen=True)
class Startable:
    """What a start records about one loaded agent beside its name and purpose."""

    agent: RoutableAgent
    run_fields: Mapping[str, Any] = field(default_factory=dict)


def intake_refusal(view: LogView, session: str) -> CommandRefused | None:
    """Why a new request may not enter ``session`` now (module doc, item 1), or None."""
    running = session_open_runs(view, session)
    if running:
        return CommandRefused(LANE_REQUIRED, f"{session} has the open run {running[0]}")
    if open_session_interrupts(view, session):
        return CommandRefused(ROUTE_PENDING, f"{session} waits for the answer to a route question")
    return None


def start_spec(
    agents: Mapping[str, Startable],
    ids: IdSource,
    sandbox: Callable[[], Availability | None],
) -> CommandSpec:
    """The ``send_message`` command that names its agent (module doc)."""

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        session = command.session
        refused = intake_refusal(view, session)
        if refused is not None:
            return refused
        name = str(command.args["agent"])
        startable = agents.get(name)
        if startable is None:
            return CommandRefused(AGENT_UNKNOWN, f"no loaded agent {name!r}")
        headless = command.args.get("headless") is True
        code = headless_start_refusal(headless, sandbox() if headless else None)
        if code is not None:
            return CommandRefused(code, "a headless run needs an available sandbox backend")
        message = ids.next(view.owner, "M")
        run = ids.next(view.owner, "R")
        said = user_message(session, message, str(command.args["text"]), name)
        decided, started = opening(session, message, startable.agent, "person", run)
        fields: dict[str, Any] = {"api_version": API_VERSION, **startable.run_fields}
        if headless:
            fields["headless"] = True
        started = replace(started, payload={**started.payload, **fields})
        events: tuple[NewEvent, ...] = (said, decided, started)
        return CommandAccepted(events)

    return CommandSpec("send_message", START_BOUNDARY, decide)
