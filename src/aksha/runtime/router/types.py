"""Request router values (ADR-P23): the alias, a routable agent, a decision, a question.

A **request router** picks the agent that handles a free-text request in a session that
names no agent. A **route decision** (``route.decided``) is the recorded fact of that
choice. A **route interrupt** is an interrupt of kind ``route``, scoped to the session,
that asks the person which agent they meant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from aksha.runtime.invalidation.modes import InvalidationMode
from aksha.runtime.models.types import ConfidenceThresholds

ROUTE_ALIAS = "runtime.route"
"""The one classification alias the runtime core owns (D1)."""

ROUTE_QUESTION = "agent"
"""The name of the route question; its options are the loaded agents' names."""

ROUTE_THRESHOLDS = ConfidenceThresholds(accept_at=0.8, margin=0.2)
"""The worked example's placeholder thresholds; unfitted (``fitted_on`` is None)."""

SUGGESTED = 3
"""How many of the highest-scoring agents a route question names first (D3)."""

RouteBy = Literal["classifier", "person"]


@dataclass(frozen=True)
class RoutableAgent:
    """A loaded agent as the router sees it: its name and its ``purpose`` line (ADR-P20 S1).

    ``definition_hash`` is recorded on the ``run.started`` the route opens, when given.
    ``reentry`` is the re-entry setting a request run of this agent records (decision 36):
    ``resume`` opts its re-entries into resuming the existing session; None and
    ``fresh`` record ``fresh``, the default. ``invalidation`` is the agent's invalidation
    mode (decision 38), recorded on the ``run.started`` the route opens when given; a run
    that records none reads as ``rebuild``, so a loaded agent always passes its mode.
    """

    name: str
    purpose: str
    definition_hash: str | None = None
    reentry: Literal["fresh", "resume"] | None = None
    invalidation: InvalidationMode | None = None

    def __post_init__(self) -> None:
        """Refuse an unknown re-entry setting."""
        if self.reentry not in (None, "fresh", "resume"):
            raise ValueError(f"unknown re-entry setting {self.reentry!r}")


@dataclass(frozen=True)
class RouteDecision:
    """Which agent handles a message, who chose, and the run it opened (D6).

    ``step_id`` is the classification step behind it; None when a person named the agent
    with the message (D5). ``probabilities_ppm`` is the classifier's distribution, highest
    first, in parts per million (ADR-P07 D4); empty when no classification ran.
    """

    message: str
    agent: str
    by: RouteBy
    run: str
    step_id: str | None = None
    probabilities_ppm: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class RoutePending:
    """The route question is open: no run opens for ``message`` until a person answers (D4)."""

    message: str
    interrupt_id: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class RouteHeld:
    """The classifier accepted, but the session is held, so the run waits (ADR-P16 D6)."""

    message: str
    agent: str
    held_by: int


RouteOutcome = RouteDecision | RoutePending | RouteHeld
"""What routing one message came to."""
