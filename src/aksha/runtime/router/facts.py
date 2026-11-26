"""The router's facts: ``user.message``, ``route.decided`` and the run it opens. Pure.

``route.decided`` is written in the transaction that opens the run (D6): with the
``user.message`` when a person named the agent (D5, boundary U1), after the
classification step when the classifier accepted (U1), and with ``interrupt.resolved``
when a person answered the route question (U4). The run carries the session in its
envelope, so the session's open runs are read from the log (D7).
"""

from __future__ import annotations

from collections.abc import Sequence

from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.router.types import RoutableAgent, RouteBy

USER_MESSAGE = "user.message"
ROUTE_DECIDED = "route.decided"
RUN_STARTED = "run.started"
REQUEST_CAUSE = "request"


def user_message(session: str, message: str, text: str, agent: str | None = None) -> NewEvent:
    """A person's request as the session received it; ``agent`` when they named one."""
    payload: dict[str, str] = {"message": message, "text": text}
    if agent is not None:
        payload["agent"] = agent
    return NewEvent(USER_MESSAGE, payload, session=session)


def route_decided(
    session: str,
    message: str,
    agent: str,
    by: RouteBy,
    run: str,
    *,
    step: str | None = None,
    probabilities_ppm: Sequence[tuple[str, int]] = (),
) -> NewEvent:
    """The ``route.decided`` fact (D6); the run is named in the payload, not the envelope."""
    payload: dict[str, object] = {"message": message, "agent": agent, "by": by, "run": run}
    if step is not None:
        payload["step"] = step
    if probabilities_ppm:
        payload["probabilities_ppm"] = dict(probabilities_ppm)
    return NewEvent(ROUTE_DECIDED, payload, session=session)


def run_opened(session: str, run: str, agent: RoutableAgent) -> NewEvent:
    """The ``run.started{cause: request}`` a route opens, in the session's envelope."""
    payload: dict[str, str] = {"agent": agent.name, "cause": REQUEST_CAUSE}
    if agent.definition_hash is not None:
        payload["definition_hash"] = agent.definition_hash
    return NewEvent(RUN_STARTED, payload, run=run, session=session)


def opening(
    session: str,
    message: str,
    agent: RoutableAgent,
    by: RouteBy,
    run: str,
    *,
    step: str | None = None,
    probabilities_ppm: Sequence[tuple[str, int]] = (),
) -> tuple[NewEvent, NewEvent]:
    """``route.decided`` and the ``run.started`` it opens, committed together (D6)."""
    decided = route_decided(session, message, agent.name, by, run, step=step,
                            probabilities_ppm=probabilities_ppm)  # fmt: skip
    return decided, run_opened(session, run, agent)
