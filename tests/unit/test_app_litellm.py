"""Choosing the LiteLLM adapters by configuration (ADR-P07 G6), live limits and the file opt-in.

LiteLLM is the default live provider: every alias of an agent binds to the LiteLLM class of
its model class, the model named in LiteLLM's form. The start is refused before any call
without a model, without LiteLLM installed, or without the provider variables LiteLLM says
the model needs (named, never read). ``AKSHA_LIVE_PROVIDER=chat-completions`` keeps the
standard-library adapters. Live mode sizes the active-time cap and the model step timeout
for a real model; fake mode keeps the agent's own. LiteLLM is a stub here; no test reaches
the network.
"""

from __future__ import annotations

import sys
from functools import cache

import pytest

from aksha.app import (
    LIVE_ACTIVE_MS,
    LIVE_TIMEOUT_MARGIN_MS,
    RuntimeConfig,
    bind_live,
    live_refusals,
    load_definitions,
    native_file_input,
    session_agent,
)
from aksha.runtime.definitions.loader import LoadedDefinition
from aksha.runtime.models.errors import BindRefused
from aksha.runtime.storage.errors import StoreError
from tests.support.litellm_stub import StubLiteLLM

MODEL = "gemini/gemini-4-neon"
LIVE = {"AKSHA_LIVE_MODEL": MODEL}


@cache
def research() -> LoadedDefinition:
    (found,) = [d for d in load_definitions(RuntimeConfig.from_env({}))
                if d.agent.name == "research"]  # fmt: skip
    return found


@pytest.fixture
def lib(monkeypatch: pytest.MonkeyPatch) -> StubLiteLLM:
    stub = StubLiteLLM()
    monkeypatch.setitem(sys.modules, "litellm", stub)
    return stub


def test_litellm_is_the_default_provider_for_every_class(lib: StubLiteLLM) -> None:
    bound = bind_live(research(), LIVE)
    kinds = {type(a).__name__ for a in bound.values()}
    assert kinds == {"LiteLLMGenerationModel", "LiteLLMExtractionModel",
                     "LiteLLMClassificationModel"}  # fmt: skip
    assert lib.calls == []  # binding asks nothing of the model


def test_a_model_per_class_and_the_chat_completions_fallback_mix(lib: StubLiteLLM) -> None:
    env = {"AKSHA_LIVE_PROVIDER_GENERATION": "chat-completions",
           "AKSHA_LIVE_BASE_URL": "http://127.0.0.1:9/v1",
           "AKSHA_LIVE_MODEL_GENERATION": "small", "AKSHA_LIVE_MODEL": MODEL}  # fmt: skip
    bound = bind_live(research(), env)
    assert type(bound["generate"]).__name__ == "LiveGenerationModel"
    others = {type(a).__name__ for n, a in bound.items() if n != "generate"}
    assert others == {"LiteLLMExtractionModel", "LiteLLMClassificationModel"}


def test_a_missing_provider_key_is_refused_by_name_before_any_call(lib: StubLiteLLM) -> None:
    lib.needs[MODEL] = ["GEMINI_API_KEY"]
    refusals = live_refusals(research(), LIVE)
    assert {r.code for r in refusals} == {"live_key_missing"}
    assert all("GEMINI_API_KEY" in r.message for r in refusals)
    with pytest.raises(BindRefused):
        bind_live(research(), LIVE)
    assert lib.calls == []


def test_no_model_an_unknown_provider_or_no_litellm_is_refused(
    lib: StubLiteLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    refusals = live_refusals(research(), {})
    assert refusals and {r.code for r in refusals} == {"live_config_missing"}
    unknown = live_refusals(research(), {**LIVE, "AKSHA_LIVE_PROVIDER": "litelm"})
    assert unknown and all(r.code == "live_config_missing" for r in unknown)
    monkeypatch.setitem(sys.modules, "litellm", None)
    (missing,) = live_refusals(research(), LIVE)
    assert missing.code == "live_config_missing" and "aksha-harness[live]" in missing.message


def test_no_provider_key_is_an_aksha_setting(lib: StubLiteLLM) -> None:
    config = RuntimeConfig.from_env({"AKSHA_MODE": "live", **LIVE, "GEMINI_API_KEY": "x" * 39,
                                     "AKSHA_NATIVE_FILES": "on"})  # fmt: skip
    assert "GEMINI_API_KEY" not in config.live_env
    assert config.live_env["AKSHA_NATIVE_FILES"] == "on"


def test_live_mode_sizes_the_active_time_and_the_model_timeout_offline_keeps_them(
    lib: StubLiteLLM,
) -> None:
    loaded = research()
    own = loaded.agent.budget
    assert own is not None and own.active_ms < LIVE_ACTIVE_MS
    fake = session_agent(loaded, "fake")
    assert fake.agent.budget == own and fake.run_fields()["budget"]["active_ms"] == own.active_ms
    live = session_agent(loaded, "live", live_env=LIVE)
    assert live.agent.budget is not None and live.agent.budget.active_ms == LIVE_ACTIVE_MS
    assert live.run_fields()["budget"]["active_ms"] == LIVE_ACTIVE_MS  # recorded on the run
    assert live.agent.limits is not None
    assert live.agent.limits.model_timeout_ms == 240_000 + LIVE_TIMEOUT_MARGIN_MS
    assert live.agent.limits.turn_limit == (loaded.agent.limits or live.agent.limits).turn_limit
    chosen = session_agent(loaded, "live", live_env={**LIVE, "AKSHA_LIVE_ACTIVE_MS": "7200000",
                                                    "AKSHA_LIVE_TIMEOUT_S": "600"})  # fmt: skip
    assert chosen.agent.budget is not None and chosen.agent.budget.active_ms == 7_200_000
    assert chosen.agent.limits is not None
    assert chosen.agent.limits.model_timeout_ms == 600_000 + LIVE_TIMEOUT_MARGIN_MS
    with pytest.raises(BindRefused):
        session_agent(loaded, "live", live_env={**LIVE, "AKSHA_LIVE_ACTIVE_MS": "an hour"})


@pytest.mark.parametrize(("setting", "enabled"), [(None, False), ("off", False), ("on", True)])
def test_intake_takes_native_files_only_when_opted_in(setting: str | None, enabled: bool) -> None:
    env = {"AKSHA_MODE": "live", **LIVE}
    if setting is not None:
        env["AKSHA_NATIVE_FILES"] = setting
    files = native_file_input(RuntimeConfig.from_env(env))
    assert files is not None and files.enabled is enabled
    assert "application/pdf" in files.accepts and files.max_inline_bytes > 0


def test_no_native_file_input_offline_or_for_a_model_without_files() -> None:
    assert native_file_input(RuntimeConfig.from_env({"AKSHA_NATIVE_FILES": "on"})) is None
    chat = {"AKSHA_MODE": "live", "AKSHA_LIVE_PROVIDER": "chat-completions",
            "AKSHA_LIVE_BASE_URL": "http://127.0.0.1:9/v1", "AKSHA_NATIVE_FILES": "on"}  # fmt: skip
    assert native_file_input(RuntimeConfig.from_env(chat)) is None
    with pytest.raises(StoreError) as e:
        native_file_input(RuntimeConfig.from_env({"AKSHA_MODE": "live", **LIVE,
                                                  "AKSHA_NATIVE_FILES": "maybe"}))  # fmt: skip
    assert e.value.code == "native_files_invalid"


@pytest.mark.parametrize(
    "setting",
    [{"AKSHA_LIVE_REASONING_EFFORT_CLASSIFICATION": "loud"},
     {"AKSHA_LIVE_TIMEOUT_S_EXTRACTION": "soon"},
     {"AKSHA_LIVE_SAMPLES": "three"},
     {"AKSHA_NATIVE_FILES": "maybe"}],
)  # fmt: skip
def test_a_malformed_live_setting_is_a_bind_refusal_naming_it(
    lib: StubLiteLLM, setting: dict[str, str]
) -> None:
    with pytest.raises(BindRefused) as refused:
        bind_live(research(), {**LIVE, **setting})
    (refusal,) = refused.value.refusals
    assert refusal.code == "live_config_missing"
    (name,) = setting
    assert name.split("_")[2] in refusal.message  # the setting's own name
    assert lib.calls == []
