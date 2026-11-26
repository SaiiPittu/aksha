"""The route question and its fake (ADR-P23 D1, D3): built from purposes, judged by code."""

from __future__ import annotations

import pytest

from aksha.runtime.models.thresholds import threshold_classification
from aksha.runtime.models.types import ConfidenceThresholds
from aksha.runtime.router.errors import RouterInvalid
from aksha.runtime.router.fake import PurposeClassifier
from aksha.runtime.router.question import (
    clarifying_question,
    option_texts,
    route_alias,
    route_question,
    route_request,
)
from aksha.runtime.router.router import Router
from aksha.runtime.router.types import (
    ROUTE_ALIAS,
    ROUTE_QUESTION,
    ROUTE_THRESHOLDS,
    RoutableAgent,
)
from tests.support.router import AGENTS, CLEAR, DISCLOSURE, PATENT, RESEARCH, UNCLEAR


def test_the_question_lists_each_agent_with_its_purpose() -> None:
    q = route_question(AGENTS, ROUTE_THRESHOLDS)
    assert (q.name, q.type, q.options) == (ROUTE_QUESTION, "choice", ("research", "patent"))
    assert option_texts(q) == {"research": RESEARCH.purpose, "patent": PATENT.purpose}
    assert q.thresholds.accept_at == 0.8 and q.thresholds.margin == 0.2


def test_the_alias_is_the_runtimes_one_classification_alias() -> None:
    alias = route_alias(AGENTS, ROUTE_THRESHOLDS)
    assert (alias.name, alias.model_class) == (ROUTE_ALIAS, "classification")
    assert alias.name == "runtime.route" and alias.questions[0].name == ROUTE_QUESTION


def test_the_state_is_the_message_only() -> None:
    """RT-1 A: no transcript enters the request; the same words give the same hash."""
    q = route_question(AGENTS, ROUTE_THRESHOLDS)
    one = route_request("session/S-1/route/M-0001", CLEAR, q)
    two = route_request("session/S-1/route/M-0001", CLEAR, q)
    assert one.state == CLEAR and one.state_inputs == ()
    assert one.request_hash == two.request_hash
    changed = route_question((RESEARCH, RoutableAgent("patent", "Draft claims.")), ROUTE_THRESHOLDS)
    assert route_request(one.step_id, CLEAR, changed).request_hash != one.request_hash


@pytest.mark.parametrize(
    ("text", "outcome", "choice"),
    [
        (CLEAR, "accept", "research"),
        (DISCLOSURE, "accept", "patent"),
        (UNCLEAR, "abstain", "research"),
    ],
)
def test_the_fake_scores_purpose_words(text: str, outcome: str, choice: str) -> None:
    q = route_question(AGENTS, ROUTE_THRESHOLDS)
    result = PurposeClassifier().classify(route_request("s", text, q))
    answer = result.answer(ROUTE_QUESTION)
    assert answer is not None and sum(p for _, p in answer.probabilities_ppm) == 1_000_000
    judged = threshold_classification(answer, q, native=True)
    assert (judged.outcome, judged.value) == (outcome, choice)


def test_the_fake_refuses_when_no_purpose_word_matches() -> None:
    q = route_question(AGENTS, ROUTE_THRESHOLDS)
    result = PurposeClassifier().classify(route_request("s", "hello there", q))
    answer = result.answer(ROUTE_QUESTION)
    assert answer is not None and answer.choice is None
    assert threshold_classification(answer, q, native=True).reason == "refused"


def test_the_margin_abstains_on_a_close_call() -> None:
    """Three purpose words against two clear ``accept_at`` 0.55; the margin of 0.5 still asks."""
    loose = ConfidenceThresholds(accept_at=0.55, margin=0.5)
    q = route_question(AGENTS, loose)
    text = "competitors and customers for the sensor idea"
    answer = PurposeClassifier().classify(route_request("s", text, q)).answer(ROUTE_QUESTION)
    judged = threshold_classification(answer, q, native=True)
    assert (judged.outcome, judged.reason, judged.value) == ("abstain", "under_margin", "research")


def test_the_clarifying_question_reads_the_top_purposes() -> None:
    text = clarifying_question(("research", "patent"), {a.name: a.purpose for a in AGENTS})
    assert text == (
        "Research competitors and customers for a sensor product, "
        "or prepare invention disclosures for a sensor idea?"
    )


@pytest.mark.parametrize(
    ("agents", "code"),
    [
        ((RESEARCH,), "route_agents_invalid"),
        ((RESEARCH, RESEARCH), "route_agents_invalid"),
        ((RESEARCH, RoutableAgent("patent", " ")), "route_agents_invalid"),
    ],
)
def test_a_router_needs_two_named_agents_with_purposes(
    agents: tuple[RoutableAgent, ...], code: str
) -> None:
    with pytest.raises(RouterInvalid) as e:
        Router(agents, PurposeClassifier(), ids=None)  # type: ignore[arg-type]
    assert e.value.code == code


def test_a_router_needs_a_classifier_with_a_distribution() -> None:
    class NoDistribution(PurposeClassifier):
        capabilities = PurposeClassifier.capabilities.__class__(
            native=True, question_types=frozenset({"choice"}), returns_distribution=False
        )

    with pytest.raises(RouterInvalid) as e:
        Router(AGENTS, NoDistribution(), ids=None)  # type: ignore[arg-type]
    assert e.value.code == "route_model_invalid"
