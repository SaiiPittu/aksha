"""Model calls as durable steps, and the fast-class cascade (ADR-P07 D6 to D8, worked example)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.models.scripted import (
    ScriptedClassificationModel,
    ScriptedExtractionModel,
    ScriptedGenerationModel,
)
from aksha.runtime.execution.types import StepId
from aksha.runtime.models.errors import ContextOverflow, PermanentModelError, TransientModelError
from aksha.runtime.models.routing import AliasTable, bind_aliases
from aksha.runtime.models.steps import generation_step, run_cascade
from aksha.runtime.models.types import (
    ClassAnswer,
    ClassificationRequest,
    ClassificationResult,
    ConfidenceThresholds,
    ExtractionRequest,
    ExtractionResult,
    FieldAnswer,
    FieldSpec,
    GenerationRequest,
    GenerationResult,
    Message,
    ModelAlias,
    QuestionSpec,
    SpanRef,
    ToolCall,
    Usage,
)
from tests.support.models import ModelHarness, floats_in, span
from tests.support.steps import Crash

TURN = StepId("R-0001", "compare[C1]", turn=3)
CALL = StepId("R-0001", "compare[C1]", turn=3, call=2)
SUPPORT_Q = QuestionSpec(
    "support", "Does claim 1 of family A disclose feature F2?", options=("yes", "partial", "no"),
    thresholds=ConfidenceThresholds(accept_at=0.9), adapted=ConfidenceThresholds(accept_at=0.85),
)  # fmt: skip
NOVEL_Q = QuestionSpec("novel", "Is F2 new?", type="boolean")
SUPPORT = ModelAlias(
    "classify.support", "classification", fallback=("classify.adapted",), allow_adapted=True,
    questions=(SUPPORT_Q, NOVEL_Q),
)  # fmt: skip
ADAPTED = ModelAlias("classify.adapted", "classification", allow_adapted=True)
PRICE_F = FieldSpec("amount", "number")
PRICE = ModelAlias("extract.price", "extraction", fields=(PRICE_F,), on_exhausted="fail")


def dist(**ppm: int) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(ppm.items(), key=lambda kv: (-kv[1], kv[0])))


def classes(*answers: tuple[str, ClassAnswer]) -> ClassificationResult:
    return ClassificationResult(answers)


@pytest.fixture
def h(tmp_path: Path) -> Iterator[ModelHarness]:
    harness = ModelHarness(tmp_path / "aksha.sqlite")
    yield harness
    harness.close()


def table(
    generation: ScriptedGenerationModel | None = None,
    native: ScriptedClassificationModel | None = None,
    adapted: ScriptedClassificationModel | None = None,
    price: ScriptedExtractionModel | None = None,
) -> AliasTable:
    return bind_aliases(
        (SUPPORT, ADAPTED, PRICE),
        "scripted",
        {
            "generate": generation or ScriptedGenerationModel([]),
            "classify.support": native or ScriptedClassificationModel([]),
            "classify.adapted": adapted or ScriptedClassificationModel([], native=False),
            "extract.price": price or ScriptedExtractionModel([]),
        },
    )


def gen_request(step: StepId = TURN) -> GenerationRequest:
    return GenerationRequest(str(step), "generate", (Message("user", "Compare C1."),))


def support_request() -> ClassificationRequest:
    return ClassificationRequest(
        str(CALL), "classify.support", "Claim 1: a sensor housing ...", (SUPPORT_Q, NOVEL_Q),
        state_inputs=("EX-0001",),
    )  # fmt: skip


# --- generation --------------------------------------------------------------------------


def test_a_generation_call_is_a_step_with_its_usage_and_replays(h: ModelHarness) -> None:
    reply = GenerationResult(
        None, (ToolCall("c1", "fetch", {"id": "acme"}),), "tool_calls", Usage(120, 30, 45)
    )
    model = ScriptedGenerationModel([reply])
    out = generation_step(h.executor(), TURN, table(generation=model), gen_request())
    assert out.code is None and out.result == reply and model.calls == [str(TURN)]
    started, completed = [e for e in h.log() if e[1] == str(TURN)]
    assert started[0] == "step.started" and started[2]["kind"] == "model"
    assert {k: started[2][k] for k in ("model_class", "alias", "native")} == {
        "model_class": "generation", "alias": "generate", "native": True,
    }  # fmt: skip
    assert started[2]["args_hash"] == gen_request().context_hash
    assert started[2]["adapter"] == "aksha.adapters.models.scripted:ScriptedGenerationModel"
    assert completed[2]["usage"] == {"input_tokens": 120, "output_tokens": 30, "cost_micro_usd": 45}
    again = generation_step(h.restart(), TURN, table(generation=model), gen_request())
    assert again.step.replayed and again.result == reply and model.calls == [str(TURN)]


def test_model_transient_then_unavailable(h: ModelHarness) -> None:
    flaky = ScriptedGenerationModel([TransientModelError(message="503")] * 3)
    out = generation_step(h.executor(), TURN, table(generation=flaky), gen_request())
    assert (out.result, out.code, out.step.attempts) == (None, "model_unavailable", 3)
    failed = [p for t, s, p in h.log() if t == "step.failed"]
    assert [f["retried"] for f in failed] == [True, True, False]
    recovers = ScriptedGenerationModel(
        [TransientModelError(), TransientModelError(), GenerationResult("done")]
    )
    step = TURN.with_turn(4)
    out = generation_step(h.executor(), step, table(generation=recovers), gen_request(step))
    assert out.result == GenerationResult("done") and out.step.attempts == 3


def test_script_exhausted_fails_loudly(h: ModelHarness) -> None:
    out = generation_step(h.executor(), TURN, table(), gen_request())
    assert (out.result, out.code) == (None, "script_exhausted")


def test_overflow_and_permanent_errors_are_not_retried(h: ModelHarness) -> None:
    model = ScriptedGenerationModel([ContextOverflow(), PermanentModelError("refused_by_policy")])
    first = generation_step(h.executor(), TURN, table(generation=model), gen_request())
    step = TURN.with_turn(4)
    second = generation_step(h.executor(), step, table(generation=model), gen_request(step))
    assert (first.code, second.code) == ("context_overflow", "refused_by_policy")
    assert first.step.attempts == second.step.attempts == 1


def test_a_request_for_another_step_is_refused(h: ModelHarness) -> None:
    with pytest.raises(ValueError):
        generation_step(h.executor(), TURN, table(), gen_request(TURN.with_turn(9)))


# --- the cascade -------------------------------------------------------------------------


def test_the_worked_example_native_abstains_then_adapted_accepts(h: ModelHarness) -> None:
    native = ScriptedClassificationModel([classes(
        ("support", ClassAnswer("partial", dist(partial=400_000, yes=350_000, no=250_000))),
        ("novel", ClassAnswer(True, dist(true=950_000, false=50_000))),
    )])  # fmt: skip
    adapted = ScriptedClassificationModel([classes(
        ("support", ClassAnswer("partial", dist(partial=900_000, yes=60_000, no=40_000))),
    )], native=False)  # fmt: skip
    out = run_cascade(h.executor(), CALL, table(native=native, adapted=adapted), support_request())
    assert out.accepted() == {"support": "partial", "novel": True}
    assert out.exhausted == () and out.action is None
    assert [j.step for j in out.judgements] == [f"{CALL}/d02", f"{CALL}/d01"]
    # the adapted candidate is asked only what the native one left unaccepted
    started = {s: p for t, s, p in h.log() if t == "step.started"}
    d01, d02 = started[f"{CALL}/d01"], started[f"{CALL}/d02"]
    assert "replaces" not in d01 and d02["replaces"] == f"{CALL}/d01"
    assert (d01["native"], d02["native"], d02["alias"]) == (True, False, "classify.adapted")
    completed = {s: p for t, s, p in h.log() if t == "step.completed"}
    assert [a["name"] for a in completed[f"{CALL}/d02"]["value"]["answers"]] == ["support"]
    assert completed[f"{CALL}/d01"]["value"]["judgements"][0]["reason"] == "under_accept"


def test_a_used_up_cascade_says_what_follows(h: ModelHarness) -> None:
    low = classes(
        ("support", ClassAnswer("partial", dist(partial=600_000, yes=300_000, no=100_000)))
    )
    native = ScriptedClassificationModel([classes(
        ("support", ClassAnswer("partial", dist(partial=400_000, yes=350_000, no=250_000))),
        ("novel", ClassAnswer(True, dist(true=950_000, false=50_000))),
    )])  # fmt: skip
    out = run_cascade(
        h.executor(), CALL,
        table(native=native, adapted=ScriptedClassificationModel([low], native=False)),
        support_request(),
    )  # fmt: skip
    assert (out.exhausted, out.action) == (("support",), "unknown")
    assert out.judgements[0].reason == "under_accept" and out.judgements[0].step == f"{CALL}/d02"
    native = ScriptedClassificationModel([classes(("support", ClassAnswer(None)),
                                                  ("novel", ClassAnswer(None)))])  # fmt: skip
    call = CALL.with_call(3)
    request = ClassificationRequest(str(call), "classify.support", "s", (SUPPORT_Q, NOVEL_Q))
    out = run_cascade(
        h.executor(), call,
        table(native=native, adapted=ScriptedClassificationModel([], native=False)),
        request, reviewable=True,
    )  # fmt: skip
    assert (out.exhausted, out.action) == (("support", "novel"), "review")
    assert [code for _, code in out.failures] == ["script_exhausted"]


def test_cascade_replays_without_calls(h: ModelHarness) -> None:
    """Kill mid-cascade; the restart returns d01's recorded answer and runs only d02."""
    first = classes(
        ("support", ClassAnswer("partial", dist(partial=400_000, yes=350_000, no=250_000))),
        ("novel", ClassAnswer(False, dist(true=100_000, false=900_000))),
    )
    good = classes(("support", ClassAnswer("yes", dist(yes=950_000, partial=30_000, no=20_000))))
    native = ScriptedClassificationModel([first])
    adapted = ScriptedClassificationModel([Crash("killed inside d02"), good], native=False)
    with pytest.raises(Crash):
        run_cascade(h.executor(), CALL, table(native=native, adapted=adapted), support_request())
    out = run_cascade(h.restart(), CALL, table(native=native, adapted=adapted), support_request())
    assert native.calls == [f"{CALL}/d01"]
    assert adapted.calls == [f"{CALL}/d02", f"{CALL}/d02"]
    assert out.steps[0].replayed and not out.steps[1].replayed
    assert out.accepted() == {"support": "yes", "novel": False}


def test_a_permanent_failure_moves_to_the_next_candidate(h: ModelHarness) -> None:
    native = ScriptedClassificationModel([PermanentModelError("refused")])
    adapted = ScriptedClassificationModel([classes(
        ("support", ClassAnswer("no", dist(no=900_000, yes=50_000, partial=50_000))),
        ("novel", ClassAnswer(True, dist(true=950_000, false=50_000))),
    )], native=False)  # fmt: skip
    out = run_cascade(h.executor(), CALL, table(native=native, adapted=adapted), support_request())
    assert out.failures == ((f"{CALL}/d01", "refused"),)
    assert out.accepted() == {"support": "no", "novel": True}


def test_an_extraction_cascade_cites_or_fails(h: ModelHarness) -> None:
    page = span()
    request = ExtractionRequest(str(CALL), "extract.price", (page,), (PRICE_F,))
    good = ExtractionResult(
        (("amount", FieldAnswer("279", (SpanRef("EX-0001", 9, 12),), 0, 970_000)),)
    )
    out = run_cascade(h.executor(), CALL, table(price=ScriptedExtractionModel([good])), request)
    assert out.accepted() == {"amount": "279"} and out.judgements[0].spans[0].start == 9
    call = CALL.with_call(3)
    request = ExtractionRequest(str(call), "extract.price", (page,), (PRICE_F,))
    guess = ExtractionResult((("amount", FieldAnswer("279", (), 0, 990_000)),))
    out = run_cascade(h.executor(), call, table(price=ScriptedExtractionModel([guess])), request)
    assert (out.exhausted, out.action) == (("amount",), "fail")
    assert out.judgements[0].reason == "span_missing"


def test_no_float_reaches_the_store(h: ModelHarness) -> None:
    native = ScriptedClassificationModel([classes(
        ("support", ClassAnswer("yes", dist(yes=950_000, partial=30_000, no=20_000))),
        ("novel", ClassAnswer(True, dist(true=950_000, false=50_000))),
    )])  # fmt: skip
    run_cascade(h.executor(), CALL, table(native=native), support_request())
    generation_step(h.executor(), TURN, table(generation=ScriptedGenerationModel(
        [GenerationResult("ok", usage=Usage(1, 1, 1))])), gen_request())  # fmt: skip
    assert [p for e in h.log() for p in floats_in(e[2])] == []


def test_a_cascade_runs_under_a_call_step_only(h: ModelHarness) -> None:
    with pytest.raises(ValueError):
        run_cascade(h.executor(), TURN, table(), support_request())
