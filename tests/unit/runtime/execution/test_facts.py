import pytest

from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.events.errors import PayloadInvalid
from aksha.runtime.execution import facts
from aksha.runtime.execution.types import DanglingStep, StepId, StepPolicy
from tests.support.steps import HASH

S = StepId("R-0001", "compare[C1]", turn=3, call=2)
CORE = Catalogue(CORE_EVENT_TYPES)


def _every_fact() -> list[tuple[str, NewEvent]]:
    once = StepPolicy(semantics="at_most_once", on_unknown="interrupt")
    dangling = DanglingStep(str(S), "R-0001", "compare[C1]", "tool", "send", "at_most_once",
                            "interrupt", dispatched=True, attempts=1)  # fmt: skip
    return [
        (
            facts.STEP_INTENT,
            facts.step_started(S, "tool", "send", HASH, once, {"route": "dispatch"}),
        ),
        (facts.STEP_INTENT, facts.step_started(S, "model", "draft", HASH, StepPolicy())),
        (facts.STEP_INTENT, facts.step_started(S, "check", "done", HASH, StepPolicy())),
        (facts.DISPATCH, facts.step_dispatched(S, 1)),
        (facts.ATTEMPT_FAILED, facts.step_failed(S, 1, "timeout", retried=True, backoff_ms=500)),
        (facts.ATTEMPT_FAILED, facts.attempt_discarded(S, 1, facts.value_hash({"a": 1}))),
        (
            facts.STEP_RESULT,
            facts.step_completed(S, "tool", "send", "ok", attempts=1, value={"a": 1}),
        ),
        (
            facts.STEP_RESULT,
            facts.step_completed(S, "model", "draft", "error", attempts=3, code="timeout"),
        ),
        (facts.STEP_RESULT, facts.unknown_completion(dangling)),
    ]


@pytest.mark.parametrize(("boundary", "event"), _every_fact())
def test_every_fact_is_a_valid_core_event_in_its_boundary(boundary: str, event: NewEvent) -> None:
    CORE.validate_payload(event.type, event.payload)
    CORE.check_boundary(event.type, boundary)
    assert (event.run, event.instance, event.step) == ("R-0001", "compare[C1]", str(S))
    assert CORE.render_dedup_key(event.type, event.context(), event.payload)


def test_dedup_keys_make_each_fact_once_per_step_and_attempt() -> None:
    keys = {CORE.render_dedup_key(e.type, e.context(), e.payload) for _, e in _every_fact()[3:6]}
    assert keys == {
        f"sdisp:{S}", f"sfail:{S}/a1", f"sdisc:{S}/a1",
    }  # fmt: skip
    assert facts.started_key(str(S)) == f"sstart:{S}"
    assert facts.completed_key(str(S)) == f"sdone:{S}"
    assert facts.dispatched_key(str(S)) == f"sdisp:{S}"


def test_unknown_completion_is_interrupted_with_outcome_unknown() -> None:
    (_, event) = _every_fact()[-1]
    assert dict(event.payload) == {
        "outcome": "interrupted", "kind": "tool", "tool": "send", "attempts": 1,
        "code": "outcome_unknown", "on_unknown": "interrupt",
    }  # fmt: skip


def test_an_error_or_interrupted_completion_needs_a_code() -> None:
    for outcome in ("error", "interrupted"):
        with pytest.raises(PayloadInvalid):
            CORE.validate_payload("step.completed", {"outcome": outcome})


def test_no_fact_holds_a_clock_reading() -> None:
    for _, event in _every_fact():
        assert not {"at", "mono_ms", "duration_ms", "late_by_ms"} & set(event.payload)
