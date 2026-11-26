"""The loader's refusals of a model alias declaration (ADR-P07 D2, D5, S1), one mutation each."""

from __future__ import annotations

from dataclasses import replace

import pytest

from aksha.runtime.api import (
    Agent,
    ConfidenceThresholds,
    FieldSpec,
    ModelAlias,
    QuestionSpec,
    artifact_keys,
    source_keys,
)
from aksha.runtime.definitions.loader import LoadedExtension, load_extensions
from aksha.runtime.definitions.validate import validate_agent
from tests.fixtures.agents.toy.agent import AGENT
from tests.support.agents import FIXTURES

MODULE = "toy/agent.py"
Q = QuestionSpec("tone", "What tone?", options=("calm", "angry"))


@pytest.fixture(scope="module")
def echo() -> tuple[LoadedExtension, ...]:
    return load_extensions(["echo"], [FIXTURES / "extensions"])


def with_alias(alias: ModelAlias) -> Agent:
    return replace(AGENT, model_aliases=(*AGENT.model_aliases, alias))


CASES: list[tuple[str, ModelAlias]] = [
    ("fields_not_extraction", ModelAlias("c2", "classification", fields=(FieldSpec("x"),))),
    ("questions_not_classification", ModelAlias("e2", "extraction", questions=(Q,))),
    ("allow_adapted_not_fast", ModelAlias("generate", "generation", allow_adapted=True)),
    (
        "adapted_gates_effect",
        ModelAlias("c3", "classification", allow_adapted=True, gates_effect=True, questions=(Q,)),
    ),
    ("field_type_unknown", ModelAlias("e4", "extraction", fields=(FieldSpec("x", "money"),))),  # type: ignore[arg-type]
    (
        "question_type_unknown",
        ModelAlias("c4", "classification", questions=(replace(Q, type="rank"),)),  # type: ignore[arg-type]
    ),
    (
        "options_exactly_one",
        ModelAlias(
            "c5",
            "classification",
            questions=(replace(Q, options_from=artifact_keys("theme_list", "themes")),),
        ),
    ),
    (
        "options_exactly_one",
        ModelAlias("c6", "classification", questions=(replace(Q, options=()),)),
    ),
    (
        "options_too_few",
        ModelAlias("c7", "classification", questions=(replace(Q, options=("calm",)),)),
    ),
    (
        "options_duplicate",
        ModelAlias("c8", "classification", questions=(replace(Q, options=("calm", "calm")),)),
    ),
    (
        "boolean_has_options",
        ModelAlias("c9", "classification", questions=(replace(Q, type="boolean"),)),
    ),
    (
        "options_from_not_artifact",
        ModelAlias(
            "c10",
            "classification",
            questions=(replace(Q, options=(), options_from=source_keys("themes")),),
        ),
    ),
    (
        "threshold_invalid",
        ModelAlias(
            "c11",
            "classification",
            questions=(replace(Q, thresholds=ConfidenceThresholds(accept_at=1.2)),),
        ),
    ),
    (
        "threshold_invalid",
        ModelAlias(
            "c12",
            "classification",
            questions=(replace(Q, thresholds=ConfidenceThresholds(accept_at=0.3, reject_at=0.3)),),
        ),
    ),
    (
        "threshold_invalid",
        ModelAlias(
            "e5",
            "extraction",
            fields=(FieldSpec("x", thresholds=ConfidenceThresholds(margin=-0.1)),),
        ),
    ),
    (
        "spec_name_duplicate",
        ModelAlias("e6", "extraction", fields=(FieldSpec("x"), FieldSpec("x"))),
    ),
    ("generate_not_generation", ModelAlias("generate", "classification", questions=(Q,))),
    ("on_exhausted_unknown", ModelAlias("e7", "extraction", on_exhausted="shrug")),  # type: ignore[arg-type]
]


@pytest.mark.parametrize(
    ("code", "alias"), CASES, ids=[f"{c}:{i}" for i, (c, _) in enumerate(CASES)]
)
def test_the_loader_refuses_an_alias(
    echo: tuple[LoadedExtension, ...], code: str, alias: ModelAlias
) -> None:
    refusals = validate_agent(with_alias(alias), module=MODULE, extensions=echo)
    assert [(r.code, r.field) for r in refusals][:1] == [(code, "model_aliases")]
    assert refusals[0].message


def test_well_formed_fast_class_aliases_pass(echo: tuple[LoadedExtension, ...]) -> None:
    agent = replace(
        AGENT,
        model_aliases=(
            *AGENT.model_aliases,
            ModelAlias("extract.price", "extraction", fields=(FieldSpec("amount", "number"),)),
            ModelAlias(
                "classify.theme",
                "classification",
                allow_adapted=True,
                questions=(
                    replace(Q, options=(), options_from=artifact_keys("theme_list", "themes")),
                    QuestionSpec("angry", "Angry?", type="boolean"),
                ),
            ),
            ModelAlias("generate", "generation"),
            ModelAlias("generate.summary", "generation", fallback=("generate",)),
        ),
    )
    assert validate_agent(agent, module=MODULE, extensions=echo) == []
