"""The request router end to end (ADR-P23): route, ask, answer, a named agent, a restart."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.runtime.execution.recovery import recover_owner
from aksha.runtime.interrupts.commands import EXPLICIT_COMMAND_REQUIRED, answer
from aksha.runtime.interrupts.reducers import interrupts, open_session_interrupts
from aksha.runtime.models.errors import TransientModelError
from aksha.runtime.models.rules import RuleClassificationModel
from aksha.runtime.models.types import ClassificationRequest, ClassificationResult
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS, open_runs
from aksha.runtime.router import router as router_module
from aksha.runtime.router import step as step_module
from aksha.runtime.router.commands import (
    AGENT_UNKNOWN,
    LANE_REQUIRED,
    ROUTE_PENDING,
    answer_route,
    send_message,
    submit,
)
from aksha.runtime.router.decisions import route_decisions, route_label, route_step_id
from aksha.runtime.router.fake import PurposeClassifier
from aksha.runtime.router.router import route_message, settle_routes
from aksha.runtime.router.types import RouteDecision, RoutePending
from aksha.runtime.session.commands import ALREADY_DECIDED
from aksha.runtime.stop_policy.hold import hold_reason
from aksha.runtime.stop_policy.types import SessionScope
from tests.support.router import (
    CLEAR,
    DISCLOSURE,
    OWNER,
    SESSION,
    UNCLEAR,
    RouterBench,
)


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[RouterBench]:
    b = RouterBench(tmp_path / "events.db")
    yield b
    b.close()


def _crash(*args: object, **kwargs: object) -> None:
    raise RuntimeError("crash")


def _runs(b: RouterBench) -> dict[str, str]:
    return {r: s.agent for r, s in rebuild(RUNS, b.view()).items()}


# --- the four tests the ADR names -----------------------------------------------------


def test_route_labels_reply(bench: RouterBench) -> None:
    """A clear request opens a run labelled with its agent (D1, D6)."""
    sent, outcome = submit(bench.store, OWNER, bench.router, SESSION, CLEAR)
    assert sent.receipt.accepted and sent.message is not None
    assert isinstance(outcome, RouteDecision)
    assert (outcome.agent, outcome.by) == ("research", "classifier")
    assert outcome.step_id == route_step_id(SESSION, sent.message)
    assert outcome.probabilities_ppm[0][0] == "research"
    assert _runs(bench) == {outcome.run: "research"}
    assert route_label(bench.view(), outcome.run) == "research"
    decided = bench.of("route.decided")
    assert decided == [{"agent": "research", "by": "classifier", "message": sent.message,
                        "run": outcome.run, "step": outcome.step_id,
                        "probabilities_ppm": dict(outcome.probabilities_ppm)}]  # fmt: skip
    started = [e for e in bench.events() if e.type == "run.started"]
    assert started[0].session == SESSION and started[0].payload["cause"] == "request"
    # route.decided and run.started commit together, after the classification step.
    assert bench.types()[-2:] == ["route.decided", "run.started"]


def test_route_abstains_and_asks(bench: RouterBench) -> None:
    """An ambiguous request raises a route interrupt and opens no run until answered (D3, D4)."""
    sent, outcome = submit(bench.store, OWNER, bench.router, SESSION, UNCLEAR)
    assert isinstance(outcome, RoutePending) and sent.message is not None
    assert outcome.options == ("research", "patent")  # equal scores keep the declared order
    assert _runs(bench) == {}
    view = bench.view()
    (held,) = open_session_interrupts(view, SESSION)
    assert held.kind == "route" and held.id == outcome.interrupt_id
    assert held.payload["message"] == sent.message
    assert set(held.payload["purposes"]) == {"research", "patent"}
    text = held.payload["questions"][0]["text"]
    assert "competitors and customers" in text and "invention disclosures" in text
    assert hold_reason(view, SessionScope(SESSION)) is not None
    # The classification step commits with the interrupt (U2): one transaction.
    completed = [e for e in bench.events() if e.type == "step.completed"]
    raised = [e for e in bench.events() if e.type == "interrupt.raised"]
    assert raised[0].seq == completed[0].seq + 1
    assert completed[0].payload["value"]["judgement"]["outcome"] == "abstain"

    receipt = answer_route(bench.store, OWNER, bench.router, outcome.interrupt_id, "patent",
                           by="person")  # fmt: skip
    assert receipt.accepted
    view = bench.view()
    assert not interrupts(view)[outcome.interrupt_id].is_open
    assert hold_reason(view, SessionScope(SESSION)) is None
    decision = route_decisions(view)[sent.message]
    assert (decision.agent, decision.by) == ("patent", "person")
    assert _runs(bench) == {decision.run: "patent"}
    # interrupt.resolved, route.decided and run.started commit in the one answer transaction.
    assert bench.types()[-4:] == [
        "command.accepted", "interrupt.resolved", "route.decided", "run.started",
    ]  # fmt: skip


def test_route_not_repeated_after_restart(
    bench: RouterBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the classification step re-uses the recorded answer (D2)."""
    sent = send_message(bench.store, OWNER, bench.router, SESSION, CLEAR)
    assert sent.message is not None
    calls: list[str] = []

    class Crashing(PurposeClassifier):
        def classify(self, request: ClassificationRequest) -> ClassificationResult:
            calls.append(request.step_id)
            return super().classify(request)

    bench.router = bench.router.with_model(Crashing())
    with monkeypatch.context() as m:
        m.setattr(router_module, "_open_run", _crash)
        with pytest.raises(RuntimeError):
            route_message(bench.store, OWNER, bench.router, SESSION, sent.message)
    assert len(calls) == 1 and "route.decided" not in bench.types()

    router = bench.restart().with_model(Crashing())
    outcome = route_message(bench.store, OWNER, router, SESSION, sent.message)
    assert isinstance(outcome, RouteDecision) and outcome.agent == "research"
    assert len(calls) == 1  # the recorded answer, not a second classification
    again = route_message(bench.store, OWNER, router, SESSION, sent.message)
    assert again == outcome and bench.types().count("run.started") == 1


def test_named_agent_skips_classifier(bench: RouterBench) -> None:
    """``SendMessage(agent=...)`` writes route.decided{by: person} and no classification (D5)."""
    _, outcome = submit(bench.store, OWNER, bench.router, SESSION, UNCLEAR, agent="patent")
    assert isinstance(outcome, RouteDecision)
    assert (outcome.agent, outcome.by, outcome.step_id) == ("patent", "person", None)
    assert "step.started" not in bench.types()
    assert bench.types() == ["command.accepted", "user.message", "route.decided", "run.started"]
    assert route_label(bench.view(), outcome.run) == "patent"
    assert bench.of("user.message")[0]["agent"] == "patent"


# --- the rest of the contract -----------------------------------------------------------


def test_a_disclosure_request_routes_by_purpose(bench: RouterBench) -> None:
    _, outcome = submit(bench.store, OWNER, bench.router, SESSION, DISCLOSURE)
    assert isinstance(outcome, RouteDecision) and outcome.agent == "patent"


def test_a_message_to_a_session_with_an_open_run_needs_a_lane(bench: RouterBench) -> None:
    """D7, ADR-P19 S2: routing applies to new requests only."""
    submit(bench.store, OWNER, bench.router, SESSION, CLEAR)
    second = send_message(bench.store, OWNER, bench.router, SESSION, DISCLOSURE)
    assert second.receipt.refused == LANE_REQUIRED and second.message is None
    named = send_message(bench.store, OWNER, bench.router, SESSION, "x", agent="patent")
    assert named.receipt.refused == LANE_REQUIRED
    other = send_message(bench.store, OWNER, bench.router, "S-0002", DISCLOSURE)
    assert other.receipt.accepted  # another session is not held


def test_a_message_while_a_route_question_is_open_is_refused(bench: RouterBench) -> None:
    submit(bench.store, OWNER, bench.router, SESSION, UNCLEAR)
    again = send_message(bench.store, OWNER, bench.router, SESSION, CLEAR)
    assert again.receipt.refused == ROUTE_PENDING
    assert bench.types().count("user.message") == 1


def test_an_unknown_named_agent_is_refused(bench: RouterBench) -> None:
    sent = send_message(bench.store, OWNER, bench.router, SESSION, CLEAR, agent="nobody")
    assert sent.receipt.refused == AGENT_UNKNOWN and "user.message" not in bench.types()


def test_a_retried_message_is_one_message(bench: RouterBench) -> None:
    one = send_message(bench.store, OWNER, bench.router, SESSION, CLEAR, command_id="c1")
    two = send_message(bench.store, OWNER, bench.router, SESSION, CLEAR, command_id="c1")
    assert two.receipt.duplicate and two.message == one.message
    assert bench.types().count("user.message") == 1


def test_the_route_answer_takes_a_loaded_agent_once(bench: RouterBench) -> None:
    _, pending = submit(bench.store, OWNER, bench.router, SESSION, UNCLEAR)
    assert isinstance(pending, RoutePending)
    wrong = answer_route(bench.store, OWNER, bench.router, pending.interrupt_id, "nobody", by="p")
    assert wrong.refused == "not_an_option"
    ok = answer_route(bench.store, OWNER, bench.router, pending.interrupt_id, "research", by="p",
                      command_id="a1")  # fmt: skip
    again = answer_route(bench.store, OWNER, bench.router, pending.interrupt_id, "patent", by="q",
                         command_id="a2")  # fmt: skip
    assert ok.accepted and again.refused == ALREADY_DECIDED
    assert bench.types().count("run.started") == 1


def test_the_generic_answer_cannot_close_a_route_question(bench: RouterBench) -> None:
    """The run opens in the answer's transaction, so only answer_route resolves it (D4)."""
    _, pending = submit(bench.store, OWNER, bench.router, SESSION, UNCLEAR)
    assert isinstance(pending, RoutePending)
    receipt = answer(bench.store, OWNER, pending.interrupt_id, by="p", text="research")
    assert receipt.refused == EXPLICIT_COMMAND_REQUIRED
    assert interrupts(bench.view())[pending.interrupt_id].is_open


def test_a_failed_classification_asks_and_never_guesses(tmp_path: Path) -> None:
    """D3: a classifier error raises the route question; no generation model is asked."""

    class Down(RuleClassificationModel):
        def classify(self, request: ClassificationRequest) -> ClassificationResult:
            raise TransientModelError(message="offline")

    b = RouterBench(tmp_path / "e.db", model=Down())
    try:
        _, outcome = submit(b.store, OWNER, b.router, SESSION, CLEAR)
        assert isinstance(outcome, RoutePending)
        completed = b.of("step.completed")[0]
        assert (completed["outcome"], completed["code"]) == ("error", "model_unavailable")
        assert open_runs(rebuild(RUNS, b.view())) == ()
    finally:
        b.close()


def test_recovery_settles_a_step_left_without_its_decision(
    bench: RouterBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2: recovery writes the decision from the recorded answer, in its place in the order."""
    sent = send_message(bench.store, OWNER, bench.router, SESSION, CLEAR)
    assert sent.message is not None
    with monkeypatch.context() as m:
        m.setattr(router_module, "_open_run", _crash)
        with pytest.raises(RuntimeError):
            route_message(bench.store, OWNER, bench.router, SESSION, sent.message)
    router = bench.restart()
    report = recover_owner(bench.store, OWNER, {}, route_check=router.route_check)
    assert report.routed == (sent.message,)
    assert route_decisions(bench.view())[sent.message].agent == "research"
    assert settle_routes(bench.store, OWNER, router) == ()  # nothing left to settle


def test_recovery_reruns_a_started_step_with_no_completion(
    bench: RouterBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A started route step with no completion is classified again (``at_least_once``)."""
    sent = send_message(bench.store, OWNER, bench.router, SESSION, CLEAR)
    assert sent.message is not None
    with monkeypatch.context() as m:
        m.setattr(step_module, "_complete", _crash)
        with pytest.raises(RuntimeError):
            route_message(bench.store, OWNER, bench.router, SESSION, sent.message)
    assert bench.types().count("step.started") == 1 and "step.completed" not in bench.types()
    router = bench.restart()
    assert settle_routes(bench.store, OWNER, router) == (sent.message,)
    assert bench.types().count("step.started") == 1
    assert bench.types().count("step.completed") == 1
    assert route_decisions(bench.view())[sent.message].agent == "research"


def test_an_open_question_is_not_settled_again(bench: RouterBench) -> None:
    submit(bench.store, OWNER, bench.router, SESSION, UNCLEAR)
    before = bench.types()
    assert settle_routes(bench.store, OWNER, bench.router) == ()
    assert bench.types() == before


def test_fake_mode_writes_the_same_bytes_twice(tmp_path: Path) -> None:
    logs = []
    for name in ("a.db", "b.db"):
        b = RouterBench(tmp_path / name)
        submit(b.store, OWNER, b.router, SESSION, UNCLEAR)
        submit(b.store, OWNER, b.router, "S-0002", CLEAR)
        logs.append([(e.type, e.step, e.session, dict(e.payload)) for e in b.events()])
        b.close()
    assert logs[0] == logs[1]
