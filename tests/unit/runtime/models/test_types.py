from aksha.runtime.models.types import (
    IMPLICIT_GENERATION_ALIAS,
    MODEL_CLASSES,
    ModelAdapterSpec,
    ModelAlias,
)


def test_three_model_classes() -> None:
    assert frozenset({"extraction", "classification", "generation"}) == MODEL_CLASSES


def test_alias_and_adapter_spec_hold_their_class() -> None:
    alias = ModelAlias("classify.tone", "classification", fallback=("classify.backup",))
    assert alias.model_class == "classification" and alias.on_exhausted is None
    spec = ModelAdapterSpec("fake", "generation", factory=object)
    assert spec.model_class == "generation"
    assert IMPLICIT_GENERATION_ALIAS == "generate"
