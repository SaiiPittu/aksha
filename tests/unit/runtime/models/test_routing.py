"""Binding aliases to adapters and the cascade rules (ADR-P07 D2, D3, D5, D9)."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from aksha.adapters.models.scripted import (
    ScriptedClassificationModel,
    ScriptedExtractionModel,
    ScriptedGenerationModel,
)
from aksha.runtime.models.errors import BindRefused
from aksha.runtime.models.routing import (
    adapter_name,
    bind_aliases,
    binding_refusal,
    cascade_for,
    live_adapters,
    live_env_name,
)
from aksha.runtime.models.types import (
    FieldSpec,
    ModelAlias,
    ModelCapabilities,
    QuestionSpec,
)

Q = QuestionSpec("support", "Disclosed?", options=("yes", "partial", "no"))
SUPPORT = ModelAlias(
    "classify.support", "classification", fallback=("classify.adapted",), allow_adapted=True,
    questions=(Q,),
)  # fmt: skip
ADAPTED = ModelAlias("classify.adapted", "classification", allow_adapted=True, questions=(Q,))
PRICE = ModelAlias("extract.price", "extraction", fields=(FieldSpec("amount", "number"),))
ALIASES = (SUPPORT, ADAPTED, PRICE)


def adapters(**changes: object) -> dict[str, object]:
    out: dict[str, object] = {
        "generate": ScriptedGenerationModel([]),
        "classify.support": ScriptedClassificationModel([]),
        "classify.adapted": ScriptedClassificationModel([], native=False),
        "extract.price": ScriptedExtractionModel([]),
    }
    out.update(changes)
    return {k: v for k, v in out.items() if v is not None}


def codes(e: pytest.ExceptionInfo[BindRefused]) -> list[tuple[str, str]]:
    return [(r.alias, r.code) for r in e.value.refusals]


def test_every_alias_and_the_implicit_generate_are_bound() -> None:
    table = bind_aliases(ALIASES, "scripted", adapters())
    assert set(table.aliases) == {
        "generate",
        "classify.support",
        "classify.adapted",
        "extract.price",
    }
    assert [(b.alias, b.native) for b in table.bindings()][:2] == [
        ("classify.adapted", False), ("classify.support", True),
    ]  # fmt: skip
    record = table.run_started_fields()
    assert record["run_mode"] == "scripted" and record["model_binding"].startswith("sha256:")
    assert record["models"][0] == {
        "alias": "classify.adapted", "model_class": "classification",
        "adapter": "aksha.adapters.models.scripted:ScriptedClassificationModel", "native": False,
    }  # fmt: skip


def test_loader_refuses_class_mismatch() -> None:
    with pytest.raises(BindRefused) as e:
        bind_aliases(
            ALIASES, "scripted", adapters(**{"extract.price": ScriptedGenerationModel([])})
        )
    assert codes(e) == [("extract.price", "model_class_mismatch")]


def test_every_refusal_is_listed_at_once() -> None:
    no_spans = ScriptedExtractionModel([], ModelCapabilities(field_types=frozenset({"number"})))
    narrow = ScriptedExtractionModel([], ModelCapabilities(returns_spans=True))
    with pytest.raises(BindRefused) as e:
        bind_aliases(ALIASES, "scripted", adapters(**{"extract.price": no_spans, "generate": None,
                                                     "classify.other": object()}))  # fmt: skip
    assert codes(e) == [("extract.price", "no_spans"), ("generate", "unbound_alias"),
                        ("classify.other", "unknown_alias")]  # fmt: skip
    with pytest.raises(BindRefused) as e:
        bind_aliases(ALIASES, "scripted", adapters(**{"extract.price": narrow}))
    assert codes(e) == [("extract.price", "unsupported_type")]
    with pytest.raises(BindRefused) as e:
        bind_aliases(ALIASES, "scripted", adapters(**{"extract.price": object()}))
    assert codes(e) == [("extract.price", "not_an_adapter")]
    with pytest.raises(BindRefused) as e:
        bind_aliases(ALIASES, "dream", adapters())  # type: ignore[arg-type]
    assert codes(e) == [("*", "mode_unknown")]


def test_an_adapted_classifier_binds_only_where_allowed() -> None:
    strict = ModelAlias("classify.support", "classification", questions=(Q,))
    adapted = ScriptedClassificationModel([], native=False)
    with pytest.raises(BindRefused) as e:
        bind_aliases((strict,), "scripted", {"generate": ScriptedGenerationModel([]),
                                             "classify.support": adapted})  # fmt: skip
    assert codes(e) == [("classify.support", "adapted_not_allowed")]


def test_offline_modes_bind_offline_adapters_only() -> None:
    class Online(ScriptedGenerationModel):
        capabilities = ModelCapabilities(native=True, offline=False)

    with pytest.raises(BindRefused) as e:
        bind_aliases((), "fake", {"generate": Online([])})
    assert codes(e) == [("generate", "not_offline")]
    assert bind_aliases((), "live", {"generate": Online([])}).mode == "live"


def test_no_generation_in_fast_class_place() -> None:
    table = bind_aliases(ALIASES, "scripted", adapters())
    for alias in ("classify.support", "classify.adapted", "extract.price"):
        chain = cascade_for(alias, table)
        assert chain and all(
            c.adapter.model_class == table.aliases[alias].model_class for c in chain
        )
    chain = cascade_for("classify.support", table)
    assert [(c.alias, c.native) for c in chain] == [
        ("classify.support", True), ("classify.adapted", False),
    ]  # fmt: skip


def test_adapted_never_gates_effect() -> None:
    gating = ModelAlias("classify.send", "classification", fallback=("classify.adapted",),
                        gates_effect=True, questions=(Q,))  # fmt: skip
    table = bind_aliases((gating, ADAPTED), "scripted", {
        "generate": ScriptedGenerationModel([]),
        "classify.send": ScriptedClassificationModel([]),
        "classify.adapted": ScriptedClassificationModel([], native=False),
    })  # fmt: skip
    assert [c.alias for c in cascade_for("classify.send", table)] == ["classify.send"]
    # without allow_adapted on the primary, the adapted fallback is dropped as well
    plain = ModelAlias("classify.plain", "classification", fallback=("classify.adapted",),
                       questions=(Q,))  # fmt: skip
    table = bind_aliases((plain, ADAPTED), "scripted", {
        "generate": ScriptedGenerationModel([]),
        "classify.plain": ScriptedClassificationModel([]),
        "classify.adapted": ScriptedClassificationModel([], native=False),
    })  # fmt: skip
    assert [c.alias for c in cascade_for("classify.plain", table)] == ["classify.plain"]


def test_natives_come_before_adapted_whatever_the_declared_order() -> None:
    first = ModelAlias("classify.a", "classification", fallback=("classify.adapted", "classify.b"),
                       allow_adapted=True, questions=(Q,))  # fmt: skip
    second = ModelAlias("classify.b", "classification", questions=(Q,))
    table = bind_aliases((first, second, ADAPTED), "scripted", {
        "generate": ScriptedGenerationModel([]),
        "classify.a": ScriptedClassificationModel([]),
        "classify.b": ScriptedClassificationModel([]),
        "classify.adapted": ScriptedClassificationModel([], native=False),
    })  # fmt: skip
    assert [c.alias for c in cascade_for("classify.a", table)] == [
        "classify.a", "classify.b", "classify.adapted",
    ]  # fmt: skip


def test_alias_table_in_the_binding_hash_and_a_resume_refused() -> None:
    table = bind_aliases(ALIASES, "scripted", adapters())
    recorded = table.binding_hash()
    assert binding_refusal(recorded, table) is None
    assert binding_refusal(None, table) == "binding_changed"

    class OtherGeneration(ScriptedGenerationModel):
        pass

    swapped = bind_aliases(ALIASES, "scripted", adapters(generate=OtherGeneration([])))
    assert swapped.binding_hash() != recorded
    assert binding_refusal(recorded, swapped) == "binding_changed"
    assert adapter_name(swapped.generation()).endswith("<locals>.OtherGeneration")


def test_the_table_hands_out_adapters_by_class_only() -> None:
    table = bind_aliases(ALIASES, "scripted", adapters())
    assert table.extraction("extract.price").model_class == "extraction"
    with pytest.raises(KeyError):
        table.generation("extract.price")
    with pytest.raises(KeyError):
        table.classification("nope")


class Live:
    model_class = "generation"
    capabilities = ModelCapabilities(native=True, offline=False)

    def __init__(self, alias: str, env: Mapping[str, str]) -> None:
        self.alias = alias
        self._key = env.get("AKSHA_LIVE_API_KEY")


def test_live_adapters_are_named_per_alias_by_the_environment() -> None:
    assert live_env_name("classify.support") == "AKSHA_ALIAS_CLASSIFY_SUPPORT"
    loaded: list[str] = []

    def load(path: str) -> type:
        loaded.append(path)
        return Live

    env = {"AKSHA_ALIAS_GENERATE": "somewhere.live:Live", "AKSHA_LIVE_API_KEY": "sk-test"}
    built = live_adapters((), env, load)
    assert loaded == ["somewhere.live:Live"] and isinstance(built["generate"], Live)
    with pytest.raises(BindRefused) as e:
        live_adapters((PRICE,), {"AKSHA_ALIAS_GENERATE": "nocolon"}, load)
    assert codes(e) == [
        ("extract.price", "live_adapter_unnamed"),
        ("generate", "live_adapter_invalid"),
    ]

    def missing(path: str) -> type:
        raise ImportError(path)

    with pytest.raises(BindRefused) as e:
        live_adapters((), {"AKSHA_ALIAS_GENERATE": "no.such:Thing"}, missing)
    assert codes(e) == [("generate", "live_adapter_invalid")]


def test_the_binding_record_fits_run_started() -> None:
    from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue

    table = bind_aliases(ALIASES, "scripted", adapters())
    payload = {"agent": "a", "cause": "request", **table.run_started_fields()}
    Catalogue(CORE_EVENT_TYPES).validate_payload("run.started", payload)
