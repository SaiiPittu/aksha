"""Fake mode is deterministic, and replay answers from the recording by step key (D7, MT-8)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from aksha.adapters.models.replay import ReplayClassificationModel, replay_adapters
from aksha.runtime.api import (
    ClassificationRequest,
    ExtractionRequest,
    FieldSpec,
    GenerationRequest,
    GenerationResult,
    Message,
    ModelAlias,
    QuestionSpec,
    RuleClassificationModel,
    RuleExtractionModel,
    RuleGenerationModel,
    ToolCall,
)
from aksha.runtime.execution.types import StepId
from aksha.runtime.models.replay import recorded_model_steps, step_key
from aksha.runtime.models.routing import AliasTable, bind_aliases
from aksha.runtime.models.steps import (
    CascadeResult,
    GenerationOutcome,
    generation_step,
    run_cascade,
)
from tests.support.models import ModelHarness, span

THEME = QuestionSpec("theme", "What is the review about?", options=("price", "support", "speed"))
ANGRY = QuestionSpec("angry", "Is the reviewer angry?", type="boolean")
ALIASES = (
    ModelAlias("extract.price", "extraction", fields=(FieldSpec("amount", "number"),
                                                      FieldSpec("currency"))),
    ModelAlias("classify.theme", "classification", questions=(THEME, ANGRY)),
)  # fmt: skip
REVIEW = "Support never answered my ticket. Furious."


class Prices(RuleExtractionModel):
    patterns: ClassVar = {"amount": r"([0-9][0-9,.]*) EUR", "currency": r"[0-9] (EUR|USD)"}


class Themes(RuleClassificationModel):
    keywords: ClassVar = {
        "theme": {"price": ("expensive", "price"), "support": ("ticket", "support")},
        "angry": {"true": ("furious", "angry")},
    }


class Policy(RuleGenerationModel):
    def generate(self, request: GenerationRequest) -> GenerationResult:
        seen = sum(1 for m in request.messages if m.role == "tool")
        if seen == 0:
            return GenerationResult(None, (ToolCall("c1", "fetch", {"id": "acme"}),), "tool_calls")
        return GenerationResult("done")


class Counting:
    """Wraps an adapter and counts calls, so a test can show replay asks nothing."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.model_class = inner.model_class
        self.capabilities = inner.capabilities
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.inner, name)

        def call(request: Any) -> Any:
            self.calls += 1
            return method(request)

        return call


def fakes() -> dict[str, Any]:
    return {
        "generate": Counting(Policy()),
        "extract.price": Counting(Prices()),
        "classify.theme": Counting(Themes()),
    }


def drive(h: ModelHarness, run: str, table: AliasTable) -> list[object]:
    """One small run: a turn, a tool result, a second turn, an extraction, a classification."""
    executor = h.executor()
    inst = "collect[acme]"
    t1, t2 = StepId(run, inst, turn=1), StepId(run, inst, turn=2)
    out: list[object] = []
    first = GenerationRequest(str(t1), "generate", (Message("user", "Price for acme?"),))
    a: GenerationOutcome = generation_step(executor, t1, table, first)
    second = GenerationRequest(str(t2), "generate", (*first.messages,
        Message("assistant", None, a.result.tool_calls if a.result else ()),
        Message("tool", '{"page": "..."}', call_id="c1")))  # fmt: skip
    b = generation_step(executor, t2, table, second)
    c1, c2 = StepId(run, inst, turn=2, call=1), StepId(run, inst, turn=2, call=2)
    page = span()
    fields = ALIASES[0].fields
    e: CascadeResult = run_cascade(
        executor, c1, table, ExtractionRequest(str(c1), "extract.price", (page,), fields)
    )
    c = run_cascade(executor, c2, table, ClassificationRequest(
        str(c2), "classify.theme", REVIEW, (THEME, ANGRY), state_inputs=("EX-0002",)))  # fmt: skip
    out += [a.result, b.result, e.accepted(), c.accepted()]
    return out


def step_values(h: ModelHarness, run: str) -> list[tuple[str, str, dict[str, Any]]]:
    keep = ("args_hash", "value", "usage", "outcome", "alias", "native", "model_class")
    return [
        (t, step_key(s), {k: p[k] for k in keep if k in p})
        for t, s, p in h.log() if s is not None and s.startswith(run) and t.startswith("step.")
    ]  # fmt: skip


def test_a_fake_mode_run_answers_by_rule(tmp_path: Path) -> None:
    h = ModelHarness(tmp_path / "a.sqlite")
    results = drive(h, "R-0001", bind_aliases(ALIASES, "fake", fakes()))
    assert results[2] == {"amount": "279", "currency": "EUR"}
    assert results[3] == {"theme": "support", "angry": True}
    h.close()


def test_two_fake_mode_runs_write_the_same_bytes(tmp_path: Path) -> None:
    one, two = ModelHarness(tmp_path / "1.sqlite"), ModelHarness(tmp_path / "2.sqlite")
    drive(one, "R-0001", bind_aliases(ALIASES, "fake", fakes()))
    drive(two, "R-0001", bind_aliases(ALIASES, "fake", fakes()))
    assert one.log() == two.log()
    one.close()
    two.close()


def test_replay_answers_from_the_recording_and_asks_no_model(tmp_path: Path) -> None:
    h = ModelHarness(tmp_path / "a.sqlite", runs=("R-0001", "R-0002"))
    recorded_results = drive(h, "R-0001", bind_aliases(ALIASES, "fake", fakes()))
    recorded = recorded_model_steps(h.store.read("o"), "R-0001")
    assert sorted(recorded) == [
        "collect[acme]/t01", "collect[acme]/t02", "collect[acme]/t02/c01/d01",
        "collect[acme]/t02/c02/d01",
    ]  # fmt: skip
    replayers = replay_adapters(ALIASES, recorded)
    table = bind_aliases(ALIASES, "replay", replayers)
    assert drive(h, "R-0002", table) == recorded_results
    assert step_values(h, "R-0002") == step_values(h, "R-0001")
    assert table.binding_hash() != bind_aliases(ALIASES, "fake", fakes()).binding_hash()
    h.close()


def test_replay_refuses_a_changed_request_and_a_missing_step(tmp_path: Path) -> None:
    h = ModelHarness(tmp_path / "a.sqlite", runs=("R-0001", "R-0002"))
    drive(h, "R-0001", bind_aliases(ALIASES, "fake", fakes()))
    table = bind_aliases(ALIASES, "replay", replay_adapters(ALIASES, recorded_model_steps(
        h.store.read("o"), "R-0001")))  # fmt: skip
    executor = h.executor()
    t1 = StepId("R-0002", "collect[acme]", turn=1)
    changed = GenerationRequest(str(t1), "generate", (Message("user", "Price for globex?"),))
    assert generation_step(executor, t1, table, changed).code == "replay_mismatch"
    t9 = StepId("R-0002", "collect[acme]", turn=9)
    missing = GenerationRequest(str(t9), "generate", (Message("user", "?"),))
    assert generation_step(executor, t9, table, missing).code == "replay_missing"
    h.close()


def test_an_adapted_recording_replays_as_adapted() -> None:
    from aksha.runtime.models.replay import RecordedStep

    recorded = {"a/t01/c01/d01": RecordedStep("a/t01/c01/d01", "classification",
                                              "classify.theme", False, "sha256:x", {})}  # fmt: skip
    built = replay_adapters(ALIASES, recorded)["classify.theme"]
    assert isinstance(built, ReplayClassificationModel) and not built.capabilities.native


def test_a_step_key_drops_the_run() -> None:
    assert step_key("R-0002/compare[C1]/t03/c02/d01") == "compare[C1]/t03/c02/d01"
    assert step_key(StepId("R-0009", "scope", turn=1)) == "scope/t01"
    with pytest.raises(Exception, match="step_id_invalid"):
        step_key("nope")
