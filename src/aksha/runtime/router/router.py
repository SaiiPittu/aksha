"""``Router``, ``route_message`` and ``settle_routes``: one message to one agent (ADR-P23).

``route_message`` routes a message that named no agent:

1. A recorded decision is returned (D2): a restart never routes a message twice.
2. An open route question is returned as :class:`RoutePending`; the session's intake is
   held (ADR-P16 D6) until a person answers it with ``commands.answer_route``.
3. Otherwise the classification runs as the message's durable step (``step.py``). An
   answer the thresholds abstain on (under ``accept_at``, or a margin under ``margin``
   between the top two agents), a refusal or a model error raises the route question in
   the step's own transaction: the agents ranked by score, the top two or three named
   first with their purpose lines (D3). No generation model is ever asked to guess.
4. An accepted answer opens the run labelled with its agent: ``route.decided{by:
   classifier}`` and ``run.started`` in one transaction (U1, D6), unless the session's
   intake is held, in which case the run waits (:class:`RouteHeld`).

``settle_routes`` is recovery's part (D2): every message with no decision and no question
is routed again, so a completed step with no decision writes the decision from the
recorded answer, and a started step with no completion classifies again
(``at_least_once``). ``Router.route_check`` is the hook ``execution.recovery`` calls.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.ports import IdSource
from aksha.runtime.interrupts.raising import raised
from aksha.runtime.interrupts.types import InterruptRequest, Question
from aksha.runtime.models.ports import ClassificationModel
from aksha.runtime.models.types import ConfidenceThresholds, QuestionSpec
from aksha.runtime.projections.view import load_view
from aksha.runtime.router.decisions import (
    messages,
    route_decisions,
    route_questions,
    route_step_id,
    unrouted,
)
from aksha.runtime.router.errors import MessageUnknown, RouterInvalid
from aksha.runtime.router.facts import opening
from aksha.runtime.router.question import clarifying_question, route_question, route_request
from aksha.runtime.router.step import RouteStep, route_step
from aksha.runtime.router.types import (
    ROUTE_THRESHOLDS,
    SUGGESTED,
    RoutableAgent,
    RouteHeld,
    RouteOutcome,
    RoutePending,
)
from aksha.runtime.stop_policy.hold import hold_reason
from aksha.runtime.stop_policy.types import SessionScope
from aksha.runtime.storage.errors import AlreadyRecorded
from aksha.runtime.storage.ports import EventStore

START = "U1"


@dataclass(frozen=True)
class Router:
    """The loaded agents, the classifier bound to ``runtime.route``, and where IDs come from.

    Refuses (``RouterInvalid``) under two agents, a name twice, an empty purpose line, or
    a model that is not a classifier returning a distribution.
    """

    agents: tuple[RoutableAgent, ...]
    model: ClassificationModel
    ids: IdSource
    thresholds: ConfidenceThresholds = ROUTE_THRESHOLDS
    question: QuestionSpec = field(init=False)

    def __post_init__(self) -> None:
        """Check the agents and the model; build the route question once."""
        names = [a.name for a in self.agents]
        if len(names) < 2 or len(set(names)) != len(names):
            raise RouterInvalid(message=f"a router needs two or more distinct agents: {names}")
        if any(not a.purpose.strip() or not a.name.strip() for a in self.agents):
            raise RouterInvalid(message="every routed agent has a name and a purpose line")
        caps = getattr(self.model, "capabilities", None)
        if (
            getattr(self.model, "model_class", None) != "classification"
            or caps is None
            or not caps.returns_distribution
            or "choice" not in caps.question_types
        ):
            raise RouterInvalid("route_model_invalid", "runtime.route needs a choice classifier")
        object.__setattr__(self, "question", route_question(self.agents, self.thresholds))

    def agent(self, name: str) -> RoutableAgent | None:
        """The loaded agent named ``name``, or None."""
        return next((a for a in self.agents if a.name == name), None)

    @property
    def purposes(self) -> dict[str, str]:
        """Each agent's purpose line, by name."""
        return {a.name: a.purpose for a in self.agents}

    def with_model(self, model: ClassificationModel) -> Router:
        """The same router over another classifier."""
        return replace(self, model=model)

    def route_check(self, store: EventStore, owner: str) -> tuple[str, ...]:
        """Recovery's hook (``execution.recovery.RouteCheck``): :func:`settle_routes`."""
        return settle_routes(store, owner, self)


def _ranked(router: Router, step: RouteStep) -> tuple[str, ...]:
    """The agents by recorded probability, highest first; ties keep declared order."""
    order = {a.name: i for i, a in enumerate(router.agents)}
    probs = dict(step.probabilities_ppm)
    return tuple(sorted(order, key=lambda n: (-probs.get(n, 0), order[n])))


def _question(router: Router, session: str, message: str, step: RouteStep) -> InterruptRequest:
    ranked = _ranked(router, step)
    suggested = ranked[:SUGGESTED]
    purposes = router.purposes
    text = clarifying_question(suggested, purposes)
    extra = {"message": message, "purposes": purposes, "suggested": list(suggested)}
    return InterruptRequest("route", session=session, questions=(Question("q1", text),),
                            options=ranked, extra=extra)  # fmt: skip


def _abstained(step: RouteStep) -> bool:
    return step.judgement is None or not step.judgement.accepted


def _open_run(
    store: EventStore, owner: str, router: Router, session: str, message: str, step: RouteStep
) -> RouteOutcome:
    """Open the run an accepted answer chose, unless the session's intake is held (D6)."""
    assert step.judgement is not None
    agent = router.agent(str(step.judgement.value))
    if agent is None:
        raise RouterInvalid(message=f"{step.step_id} chose {step.judgement.value!r}, not loaded")
    held = hold_reason(load_view(store, owner), SessionScope(session))
    if held is not None:
        return RouteHeld(message, agent.name, held.by)
    run = router.ids.next(owner, "R")
    events = opening(session, message, agent, "classifier", run, step=step.step_id,
                     probabilities_ppm=step.probabilities_ppm)  # fmt: skip
    with contextlib.suppress(AlreadyRecorded):
        store.append(owner, START, events)
    return route_decisions(load_view(store, owner))[message]


def route_message(
    store: EventStore, owner: str, router: Router, session: str, message: str
) -> RouteOutcome:
    """Route ``message`` of ``session`` (module doc). ``MessageUnknown`` if there is none."""
    view = load_view(store, owner)
    found = messages(view).get(message)
    if found is None or found.session != session:
        raise MessageUnknown(message=f"{session} holds no message {message}")
    decided = route_decisions(view).get(message)
    if decided is not None:
        return decided
    asked = route_questions(view).get(message)
    if asked is not None:
        return RoutePending(message, asked.id, tuple(str(o) for o in asked.payload["options"]))
    request = route_request(route_step_id(session, message), found.text, router.question)

    def facts(step: RouteStep) -> Sequence[NewEvent]:
        if not _abstained(step):
            return ()
        ident = router.ids.next(owner, "I")
        return (raised(_question(router, session, message, step), ident, step=step.step_id),)

    step = route_step(store, owner, session, request, router.model, facts)
    if _abstained(step):
        asked = route_questions(load_view(store, owner))[message]
        return RoutePending(message, asked.id, tuple(str(o) for o in asked.payload["options"]))
    return _open_run(store, owner, router, session, message, step)


def settle_routes(store: EventStore, owner: str, router: Router) -> tuple[str, ...]:
    """Route every message left with no decision and no question (D2); return their IDs.

    A message whose run waits on a held session is not counted as settled.
    """
    settled: list[str] = []
    for m in unrouted(load_view(store, owner)):
        outcome = route_message(store, owner, router, m.session, m.message)
        if not isinstance(outcome, RouteHeld):
            settled.append(m.message)
    return tuple(settled)
