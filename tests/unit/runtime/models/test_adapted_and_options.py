"""The adapted classifier (ADR-P07 D5) and options read from a saved artifact (S1)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from aksha.adapters.models.scripted import ScriptedClassificationModel, ScriptedGenerationModel
from aksha.runtime.execution.types import StepId
from aksha.runtime.models.adapted import ANSWER_TOOL, AdaptedClassificationModel
from aksha.runtime.models.errors import OptionsUnavailable
from aksha.runtime.models.options import resolve_questions
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.steps import run_cascade
from aksha.runtime.models.types import (
    ClassAnswer,
    ClassificationRequest,
    ClassificationResult,
    GenerationRequest,
    GenerationResult,
    ModelAlias,
    QuestionSpec,
    ToolCall,
    Usage,
)
from aksha.runtime.tasks.types import artifact_keys
from tests.support.models import ModelHarness, floats_in
from tests.support.tasks import TaskLog

SUPPORT = QuestionSpec("support", "Disclosed?", options=("yes", "partial", "no"))
NOVEL = QuestionSpec("novel", "New?", type="boolean")
REQUEST = ClassificationRequest("R-0001/a/t01/c01/d02", "classify.support", "Claim 1 ...",
                                (SUPPORT, NOVEL))  # fmt: skip


def reply(arguments: Mapping[str, object], usage: Usage | None = None) -> GenerationResult:
    call = ToolCall("c1", ANSWER_TOOL, arguments)
    return GenerationResult(None, (call,), "tool_calls", usage or Usage())


def test_the_adapted_classifier_reads_a_forced_choice_and_converts_at_its_edge() -> None:
    support = {"choice": "partial", "probabilities": {"yes": 0.05, "partial": 0.9, "no": 0.05}}
    novel = {"choice": False, "probabilities": {"true": 0.1, "false": 0.9}}
    answers = {"answers": {"support": support, "novel": novel}}
    gen = ScriptedGenerationModel([reply(answers, Usage(10, 5, 1))])
    model = AdaptedClassificationModel(gen)
    assert not model.capabilities.native and model.capabilities.offline
    out = model.classify(REQUEST)
    assert out.answer("support") == ClassAnswer(
        "partial", (("partial", 900_000), ("no", 50_000), ("yes", 50_000))
    )
    assert out.answer("novel") == ClassAnswer(False, (("false", 900_000), ("true", 100_000)))
    assert out.usage == Usage(10, 5, 1)


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {
            "answers": {
                "support": {"choice": "maybe", "probabilities": {"yes": 1, "partial": 0, "no": 0}}
            }
        },
        {"answers": {"support": {"choice": "yes", "probabilities": {"yes": 1}}}},
        {
            "answers": {
                "support": {"choice": "yes", "probabilities": {"yes": -1, "partial": 1, "no": 1}}
            }
        },
        {"answers": {"support": "yes"}},
    ],
)
def test_an_unusable_adapted_answer_is_a_refusal(arguments: dict[str, object]) -> None:
    out = AdaptedClassificationModel(ScriptedGenerationModel([reply(arguments)])).classify(REQUEST)
    assert out.answer("support") == ClassAnswer(None)
    assert out.answer("novel") == ClassAnswer(None)


def test_the_forced_tool_lists_the_options() -> None:
    seen: list[GenerationRequest] = []

    class Seeing(ScriptedGenerationModel):
        def generate(self, request: GenerationRequest) -> GenerationResult:
            seen.append(request)
            return super().generate(request)

    AdaptedClassificationModel(Seeing([reply({})])).classify(REQUEST)
    (asked,) = seen
    assert asked.tool_choice == ANSWER_TOOL and [t.name for t in asked.tools] == [ANSWER_TOOL]
    props = asked.tools[0].schema["properties"]["answers"]["properties"]
    assert props["support"]["properties"]["choice"] == {"enum": ["yes", "partial", "no"]}
    assert props["novel"]["properties"]["choice"] == {"type": "boolean"}
    assert "Disclosed? (yes, partial, no)" in (asked.messages[1].text or "")


def test_an_adapted_classifier_stands_in_through_the_cascade(tmp_path: Path) -> None:
    h = ModelHarness(tmp_path / "a.sqlite")
    alias = ModelAlias("classify.support", "classification", fallback=("classify.adapted",),
                       allow_adapted=True, questions=(SUPPORT,))  # fmt: skip
    adapted_alias = ModelAlias("classify.adapted", "classification", allow_adapted=True)
    low = (("partial", 400_000), ("yes", 350_000), ("no", 250_000))
    native = ScriptedClassificationModel(
        [ClassificationResult((("support", ClassAnswer("partial", low)),))]
    )
    support = {"choice": "partial", "probabilities": {"yes": 0.03, "partial": 0.95, "no": 0.02}}
    gen = ScriptedGenerationModel([reply({"answers": {"support": support}}, Usage(7, 3, 2))])
    table = bind_aliases((alias, adapted_alias), "scripted", {
        "generate": ScriptedGenerationModel([]),
        "classify.support": native,
        "classify.adapted": AdaptedClassificationModel(gen),
    })  # fmt: skip
    call = StepId("R-0001", "a", turn=1, call=1)
    request = ClassificationRequest(str(call), "classify.support", "Claim 1 ...", (SUPPORT,))
    out = run_cascade(h.executor(), call, table, request)
    assert out.accepted() == {"support": "partial"}
    completed = {s: p for t, s, p in h.log() if t == "step.completed"}
    assert completed[f"{call}/d02"]["usage"] == {"input_tokens": 7, "output_tokens": 3,
                                                  "cost_micro_usd": 2}  # fmt: skip
    assert "usage" not in completed[f"{call}/d01"]
    assert [p for e in h.log() for p in floats_in(e[2])] == []
    h.close()


def test_options_from_a_saved_artifact_are_recorded_with_lineage() -> None:
    log = TaskLog()
    theme = QuestionSpec("theme", "About?", options_from=artifact_keys("themes", "items"))
    with pytest.raises(OptionsUnavailable):
        resolve_questions(log.view(), (theme, NOVEL), "demo")
    log.save("themes", None, {"items": [{"key": "price"}]})
    with pytest.raises(OptionsUnavailable):
        resolve_questions(log.view(), (theme,), "demo")
    log.save("themes", None, {"items": [{"key": "support"}, {"key": "price"}, {"key": "speed"}]})
    questions, inputs = resolve_questions(log.view(), (theme, NOVEL), "demo")
    assert (
        questions[0].options == ("price", "speed", "support") and questions[0].options_from is None
    )
    assert questions[1] is NOVEL
    assert inputs == ("demo.themes@v2",)
    first = ClassificationRequest("R-0001/a/t01/c01", "classify.theme", "s", questions, inputs)
    log.save("themes", None, {"items": [{"key": "price"}, {"key": "bugs"}]})
    again, inputs2 = resolve_questions(log.view(), (theme,), "demo")
    second = ClassificationRequest("R-0001/a/t01/c01", "classify.theme", "s", again, inputs2)
    assert first.request_hash != second.request_hash
