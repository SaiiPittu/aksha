"""The router read from the log (ADR-P23 D2, D6, D7). Every function is pure.

1. :func:`route_step_id`: the classification step's ID in the session's routing namespace,
   ``session/<session>/route/<message>``, the session-scoped form of ADR-P02 D2.
2. :func:`messages`: every ``user.message`` by message ID, with its session.
3. :func:`route_decisions` and :func:`route_label`: each message's decision, and the agent
   a run's replies are labelled with (D6, "show which agent handles the request").
4. :func:`session_open_runs`: the open runs a session's routes opened (D7).
5. :func:`route_questions`: the route interrupt raised for each message, open or closed.
6. :func:`unrouted`: the messages recovery still has to settle, in log order.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from aksha.runtime.interrupts.reducers import interrupts
from aksha.runtime.interrupts.types import InterruptView
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS
from aksha.runtime.projections.view import LogView
from aksha.runtime.router.facts import ROUTE_DECIDED, RUN_STARTED, USER_MESSAGE
from aksha.runtime.router.types import RouteBy, RouteDecision


def route_step_id(session: str, message: str) -> str:
    """The deterministic step ID of a message's classification (D2)."""
    return f"session/{session}/route/{message}"


@dataclass(frozen=True)
class Message:
    """One request a session received: its ID, text, the agent it named, its log position."""

    message: str
    session: str
    text: str
    agent: str | None
    seq: int


def messages(view: LogView) -> Mapping[str, Message]:
    """Every ``user.message`` of the owner by message ID, in log order."""
    out: dict[str, Message] = {}
    for e in view.events:
        if e.type != USER_MESSAGE or e.session is None:
            continue
        p = e.payload
        named = p.get("agent")
        ident = str(p["message"])
        out[ident] = Message(ident, e.session, str(p["text"]),
                             None if named is None else str(named), e.seq)  # fmt: skip
    return MappingProxyType(out)


def route_decisions(view: LogView) -> Mapping[str, RouteDecision]:
    """Each routed message's decision, by message ID."""
    out: dict[str, RouteDecision] = {}
    for e in view.events:
        if e.type != ROUTE_DECIDED:
            continue
        p = e.payload
        probs = p.get("probabilities_ppm") or {}
        ranked = tuple(sorted(((str(k), int(v)) for k, v in probs.items()),
                              key=lambda kv: (-kv[1], kv[0])))  # fmt: skip
        step = p.get("step")
        by: RouteBy = "person" if p["by"] == "person" else "classifier"
        out[str(p["message"])] = RouteDecision(
            str(p["message"]), str(p["agent"]), by, str(p["run"]),
            None if step is None else str(step), ranked,
        )  # fmt: skip
    return MappingProxyType(out)


def route_label(view: LogView, run: str) -> str | None:
    """The agent a run's replies are labelled with: its route decision's agent (D6)."""
    for decision in route_decisions(view).values():
        if decision.run == run:
            return decision.agent
    return None


def session_open_runs(view: LogView, session: str) -> tuple[str, ...]:
    """The runs opened in ``session`` that have not ended, in start order (D7)."""
    runs = rebuild(RUNS, view)
    started = [e.run for e in view.events
               if e.type == RUN_STARTED and e.session == session and e.run is not None]  # fmt: skip
    return tuple(r for r in started if r in runs and runs[r].outcome is None)


def route_questions(view: LogView) -> Mapping[str, InterruptView]:
    """The route interrupt raised for each message, by message ID (open or closed)."""
    out: dict[str, InterruptView] = {}
    for i in interrupts(view).values():
        message = i.payload.get("message")
        if i.kind == "route" and message is not None:
            out[str(message)] = i
    return MappingProxyType(out)


def unrouted(view: LogView) -> tuple[Message, ...]:
    """Messages that named no agent, with no decision and no route question yet (D2)."""
    decided = route_decisions(view)
    asked = route_questions(view)
    return tuple(
        m for m in messages(view).values()
        if m.agent is None and m.message not in decided and m.message not in asked
    )  # fmt: skip
