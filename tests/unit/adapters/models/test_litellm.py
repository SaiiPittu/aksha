"""The LiteLLM adapters against a stubbed LiteLLM module (ADR-P07 G6).

Each class through :class:`~tests.support.litellm_stub.StubLiteLLM`: what is passed to
``completion`` (messages, tools, a forced tool, a JSON schema for decide and extract, the
timeout, no retries of LiteLLM's own, the reasoning effort, never a key), the answers read
back into the owned contracts, usage and LiteLLM's cost estimate recorded on the step, the
error mapping with the provider's error type and status on ``step.failed``, and native
file input, which is off unless opted in. No test reaches the network; values are synthetic.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aksha.adapters.models import litellm as lite
from aksha.adapters.models.litellm import (
    MAX_INLINE_BYTES,
    LiteLLMClassificationModel,
    LiteLLMExtractionModel,
    LiteLLMGenerationModel,
)
from aksha.app import LIVE_BACKOFF, LIVE_JITTER, LIVE_RETRIES
from aksha.runtime.errors import AkshaError
from aksha.runtime.execution.types import StepId
from aksha.runtime.grounding.types import CitationSpan
from aksha.runtime.models.errors import ContextOverflow, PermanentModelError, TransientModelError
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.steps import MODEL_STEP_POLICY, generation_step
from aksha.runtime.models.types import (
    ClassificationRequest,
    ExtractionRequest,
    FieldSpec,
    FilePart,
    GenerationRequest,
    Message,
    QuestionSpec,
    ToolCall,
    Usage,
)
from aksha.runtime.tools.types import ToolView
from tests.support import litellm_stub as stub_errors
from tests.support.events import START
from tests.support.litellm_stub import StubLiteLLM, json_message, message
from tests.support.models import ModelHarness, floats_in

KEY = "AIza" + "SyD-test-0000-0000-never-stored-000"  # built, so no literal looks like a key
MODEL = "gemini/gemini-4-neon"
ENV = {"AKSHA_LIVE_MODEL": MODEL}
STEP = StepId("R-0001", "a", turn=1)
FETCH = ToolView(
    "fetch", "Fetch a page.",
    {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"],
     "additionalProperties": False},
)  # fmt: skip
REQUEST = GenerationRequest(
    str(STEP), "generate",
    (Message("system", "Be brief."), Message("user", "Fetch acme."),
     Message("assistant", None, (ToolCall("c1__thought__SIG", "fetch", {"id": "acme"}),)),
     Message("tool", '{"ok": true}', call_id="c1__thought__SIG"),
     Message("user", "Now globex.")),
    tools=(FETCH,),
    tool_choice="fetch",
)  # fmt: skip
CALL = {"id": "call_9__thought__SIG-2", "type": "function",
        "function": {"name": "fetch", "arguments": '{"id": "globex", "weight": 0.5}'}}  # fmt: skip


@pytest.fixture
def lib() -> StubLiteLLM:
    return StubLiteLLM()


# --- generation --------------------------------------------------------------------------


def test_generation_passes_messages_and_tools_and_reads_calls_usage_and_cost(
    tmp_path: Path, lib: StubLiteLLM
) -> None:
    lib.answers.append(message(None, [CALL], finish="tool_calls", prompt=120, completion=40,
                               cost=0.000123))  # fmt: skip
    model = LiteLLMGenerationModel("generate", ENV, lib)
    h = ModelHarness(tmp_path / "a.sqlite")
    table = bind_aliases((), "live", {"generate": model})
    out = generation_step(h.executor(), STEP, table, REQUEST)
    assert out.result is not None
    (call,) = out.result.tool_calls
    assert (call.call_id, call.name, dict(call.arguments)) == (
        "call_9__thought__SIG-2", "fetch", {"id": "globex", "weight": "0.5"})  # fmt: skip
    assert out.result.finish == "tool_calls"
    assert out.result.usage == Usage(120, 40, 123)
    (sent,) = lib.calls
    assert sent["model"] == MODEL and sent["num_retries"] == 0 and sent["timeout"] == 240.0
    assert "api_key" not in sent and "reasoning_effort" not in sent and "api_base" not in sent
    assert [m["role"] for m in sent["messages"]] == ["system", "user", "assistant", "tool", "user"]
    # The call ID is sent back as recorded, so a provider's state in it survives a restart.
    assert sent["messages"][2]["tool_calls"][0]["id"] == "c1__thought__SIG"
    assert sent["messages"][3]["tool_call_id"] == "c1__thought__SIG"
    (tool,) = sent["tools"]
    assert tool["function"]["name"] == "fetch" and tool["function"]["parameters"]["required"]
    assert sent["tool_choice"] == {"type": "function", "function": {"name": "fetch"}}
    done = [p for t, s, p in h.log() if t == "step.completed" and s == str(STEP)]
    assert done[0]["usage"] == {"input_tokens": 120, "output_tokens": 40, "cost_micro_usd": 123}
    assert [p for e in h.log() for p in floats_in(e[2])] == []
    h.close()


def test_timeout_and_reasoning_effort_are_settings_per_alias_then_class(
    lib: StubLiteLLM,
) -> None:
    lib.answers.append(message("ok"))
    env = {**ENV, "AKSHA_LIVE_TIMEOUT_S": "30", "AKSHA_LIVE_TIMEOUT_S_GENERATE": "600",
           "AKSHA_LIVE_REASONING_EFFORT": "low",
           "AKSHA_LIVE_REASONING_EFFORT_GENERATION": "high",
           "AKSHA_LIVE_BASE_URL": "http://127.0.0.1:9/v1"}  # fmt: skip
    assert LiteLLMGenerationModel("generate", env, lib).generate(REQUEST).text == "ok"
    sent = lib.calls[0]
    assert (sent["timeout"], sent["reasoning_effort"]) == (600.0, "high")
    assert sent["api_base"] == "http://127.0.0.1:9/v1"
    fast = LiteLLMExtractionModel("page_facts", ENV, lib)
    assert fast._timeout_s == 90.0 and fast._effort is None  # fast classes default to 90 s
    for bad in ({"AKSHA_LIVE_TIMEOUT_S": "0"}, {"AKSHA_LIVE_TIMEOUT_S": "soon"},
                {"AKSHA_LIVE_REASONING_EFFORT": "max"}):  # fmt: skip
        with pytest.raises(PermanentModelError) as e:
            LiteLLMGenerationModel("generate", {**ENV, **bad}, lib)
        assert e.value.code == "live_config_missing"


def test_no_model_is_refused_before_any_call(lib: StubLiteLLM) -> None:
    with pytest.raises(PermanentModelError) as e:
        LiteLLMGenerationModel("generate", {}, lib).generate(REQUEST)
    assert e.value.code == "live_config_missing" and lib.calls == []


# --- errors ------------------------------------------------------------------------------

UNAVAILABLE = (
    'litellm.ServiceUnavailableError: GeminiException - {\n  "error": {\n    "code": 503,\n'
    '    "message": "This model is currently experiencing high demand.",\n'
    '    "status": "UNAVAILABLE"\n  }\n}'
)


def test_a_rate_limit_and_a_5xx_are_retried_by_the_step_with_the_provider_error_recorded(
    tmp_path: Path, lib: StubLiteLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    lib.answers += [
        stub_errors.RateLimitError(f"quota exceeded for key={KEY}"),
        stub_errors.ServiceUnavailableError(UNAVAILABLE),
        message("ok"),
    ]
    h = ModelHarness(tmp_path / "a.sqlite")
    table = bind_aliases((), "live", {"generate": LiteLLMGenerationModel("generate", ENV, lib)})
    out = generation_step(h.executor(), STEP, table, REQUEST)
    assert out.result is not None and out.result.text == "ok" and len(lib.calls) == 3
    failed = [p for t, s, p in h.log() if t == "step.failed"]
    assert [(p["code"], p["retried"]) for p in failed] == [("model_transient", True)] * 2
    assert failed[0]["error"]["type"] == "RateLimitError"
    assert (failed[0]["error"]["status"], failed[0]["error"]["provider"]) == (429, "gemini")
    assert failed[1]["error"] == {
        "type": "ServiceUnavailableError", "status": 503, "provider": "gemini",
        "message": "This model is currently experiencing high demand."}  # fmt: skip
    h.close()
    assert KEY.encode() not in (tmp_path / "a.sqlite").read_bytes()


def test_a_permanent_refusal_ends_the_step_with_the_provider_error_recorded(
    tmp_path: Path, lib: StubLiteLLM
) -> None:
    lib.answers.append(stub_errors.NotFoundError('{"error": {"message": "models/x is not found"}}'))
    h = ModelHarness(tmp_path / "a.sqlite")
    table = bind_aliases((), "live", {"generate": LiteLLMGenerationModel("generate", ENV, lib)})
    out = generation_step(h.executor(), STEP, table, REQUEST)
    assert out.result is None and out.code == "live_refused"
    (done,) = [p for t, s, p in h.log() if t == "step.completed"]
    assert done["error"] == {"type": "NotFoundError", "status": 404, "provider": "gemini",
                             "message": "models/x is not found"}  # fmt: skip
    h.close()


@pytest.mark.parametrize(
    ("raised", "error", "code"),
    [
        (stub_errors.RateLimitError("slow down"), TransientModelError, "model_transient"),
        (stub_errors.InternalServerError("boom"), TransientModelError, "model_transient"),
        (stub_errors.Timeout("took too long"), TransientModelError, "model_transient"),
        (stub_errors.APIConnectionError("reset"), TransientModelError, "model_transient"),
        (stub_errors.AuthenticationError("API key not valid"), PermanentModelError,
         "live_unauthorised"),
        (stub_errors.ContextWindowExceededError("too many tokens"), ContextOverflow,
         "context_overflow"),
        (stub_errors.ContentPolicyViolationError("blocked"), PermanentModelError,
         "live_blocked"),
        (stub_errors.BadRequestError("bad schema"), PermanentModelError, "live_refused"),
        (ValueError("not a LiteLLM error"), PermanentModelError, "live_refused"),
    ],
)  # fmt: skip
def test_litellm_errors_map_to_the_model_errors(
    lib: StubLiteLLM, raised: Exception, error: type[AkshaError], code: str
) -> None:
    lib.answers.append(raised)
    with pytest.raises(error) as e:
        LiteLLMGenerationModel("generate", ENV, lib).generate(REQUEST)
    assert e.value.code == code
    assert e.value.detail is not None and e.value.detail["type"] == type(raised).__name__  # type: ignore[attr-defined]


def test_a_key_in_a_provider_message_is_never_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_PROVIDER_API_KEY", "plain-secret-value-123")
    raised = stub_errors.AuthenticationError(
        f"bad key {KEY} and plain-secret-value-123 and Bearer abc.def and sk-test-abcdefghijklmnop"
    )
    got = lite.failure(raised)
    text = got.args[0] + json.dumps(got.detail)  # type: ignore[attr-defined]
    for secret in (KEY, "plain-secret-value-123", "abc.def", "sk-test-abcdefghijklmnop"):
        assert secret not in text


# --- live retries: patient, bounded, only for what a retry can fix (ADR-P14 B2) -----------

LIVE_POLICY = replace(
    MODEL_STEP_POLICY,
    max_retries=LIVE_RETRIES,
    backoff=LIVE_BACKOFF,
    jitter=LIVE_JITTER,
    jitter_source=lambda: 0.0,
)
"""The model step's policy in live mode (``app.live_limits``), its jitter drawn as none."""


def _waits(h: ModelHarness) -> list[tuple[bool, int]]:
    return [(p["retried"], p["backoff_ms"]) for t, s, p in h.log() if t == "step.failed"]


def test_a_provider_spike_is_waited_out_with_growing_backoff_each_attempt_recorded(
    tmp_path: Path, lib: StubLiteLLM
) -> None:
    lib.answers += [stub_errors.ServiceUnavailableError(UNAVAILABLE),
                    stub_errors.Timeout("Request timed out"),
                    stub_errors.ServiceUnavailableError(UNAVAILABLE),
                    stub_errors.APIConnectionError("Connection reset by peer"),
                    stub_errors.InternalServerError("internal error"),
                    message("ok")]  # fmt: skip
    h = ModelHarness(tmp_path / "a.sqlite")
    table = bind_aliases((), "live", {"generate": LiteLLMGenerationModel("generate", ENV, lib)})
    out = generation_step(h.executor(), STEP, table, REQUEST, policy=LIVE_POLICY)
    assert out.result is not None and out.result.text == "ok" and len(lib.calls) == 6
    assert _waits(h) == [(True, 2_000), (True, 4_000), (True, 8_000), (True, 16_000),
                         (True, 32_000)]  # fmt: skip
    errors = [p["error"] for t, s, p in h.log() if t == "step.failed"]
    assert [e["type"] for e in errors] == ["ServiceUnavailableError", "Timeout",
                                           "ServiceUnavailableError", "APIConnectionError",
                                           "InternalServerError"]  # fmt: skip
    assert errors[0]["status"] == 503 and errors[0]["message"].endswith("high demand.")
    assert (h.clock.now() - START).total_seconds() == 62.0  # on the injected clock
    h.close()


def test_a_spike_longer_than_the_retries_ends_the_step_unavailable(
    tmp_path: Path, lib: StubLiteLLM
) -> None:
    lib.answers += [stub_errors.ServiceUnavailableError(UNAVAILABLE) for _ in range(9)]
    h = ModelHarness(tmp_path / "a.sqlite")
    table = bind_aliases((), "live", {"generate": LiteLLMGenerationModel("generate", ENV, lib)})
    out = generation_step(h.executor(), STEP, table, REQUEST, policy=LIVE_POLICY)
    assert out.result is None and out.code == "model_unavailable"
    assert len(lib.calls) == LIVE_RETRIES + 1
    assert _waits(h)[-1] == (False, 0) and _waits(h)[4] == (True, 32_000)
    assert _waits(h)[5] == (True, 60_000)  # 64 s, capped
    h.close()


def test_the_provider_s_retry_after_is_honoured(tmp_path: Path, lib: StubLiteLLM) -> None:
    lib.answers += [stub_errors.RateLimitError("quota", headers={"Retry-After": "21"}),
                    stub_errors.RateLimitError("quota", litellm_headers={"retry-after-ms": "9500"}),
                    stub_errors.RateLimitError(GEMINI_RETRY_INFO),
                    message("ok")]  # fmt: skip
    h = ModelHarness(tmp_path / "a.sqlite")
    table = bind_aliases((), "live", {"generate": LiteLLMGenerationModel("generate", ENV, lib)})
    out = generation_step(h.executor(), STEP, table, REQUEST, policy=LIVE_POLICY)
    assert out.result is not None and len(lib.calls) == 4
    assert _waits(h) == [(True, 21_000), (True, 9_500), (True, 37_000)]
    h.close()


@pytest.mark.parametrize("raised", [
    stub_errors.AuthenticationError("API key not valid"),
    stub_errors.BadRequestError("Invalid JSON payload received"),
    stub_errors.NotFoundError("models/x is not found"),
    stub_errors.ContextWindowExceededError("too many tokens"),
    stub_errors.ContentPolicyViolationError("blocked"),
])  # fmt: skip
def test_an_auth_or_bad_request_error_is_never_retried(
    tmp_path: Path, lib: StubLiteLLM, raised: Exception
) -> None:
    lib.answers += [raised, message("never asked")]
    h = ModelHarness(tmp_path / "a.sqlite")
    table = bind_aliases((), "live", {"generate": LiteLLMGenerationModel("generate", ENV, lib)})
    out = generation_step(h.executor(), STEP, table, REQUEST, policy=LIVE_POLICY)
    assert out.result is None and out.code != "model_unavailable"
    assert len(lib.calls) == 1 and _waits(h) == []
    h.close()


def test_retries_stop_before_the_wait_passes_the_run_s_active_time(
    tmp_path: Path, lib: StubLiteLLM
) -> None:
    lib.answers += [stub_errors.ServiceUnavailableError(UNAVAILABLE) for _ in range(9)]
    h = ModelHarness(tmp_path / "a.sqlite")
    table = bind_aliases((), "live", {"generate": LiteLLMGenerationModel("generate", ENV, lib)})
    policy = replace(LIVE_POLICY, wait_cap_ms=10_000)
    out = generation_step(h.executor(), STEP, table, REQUEST, policy=policy)
    assert out.result is None and out.code == "model_unavailable"
    assert _waits(h) == [(True, 2_000), (True, 4_000), (False, 0)]  # 2 + 4 + 8 s >= 10 s
    assert (h.clock.now() - START).total_seconds() == 6.0
    h.close()


GEMINI_RETRY_INFO = (
    'litellm.RateLimitError: VertexAIException - {"error": {"code": 429, "details": [{"@type":'
    ' "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}], "status":'
    ' "RESOURCE_EXHAUSTED"}}'
)


@pytest.mark.parametrize(("raised", "wait"), [
    (stub_errors.RateLimitError("x", headers={"retry-after": "3"}), 3_000),
    (stub_errors.RateLimitError("x", headers={"Retry-After": "1.5"}), 1_500),
    (stub_errors.RateLimitError("x", headers={"retry-after-ms": "250", "retry-after": "9"}), 250),
    (stub_errors.RateLimitError("x", headers={"Retry-After": "Fri, 27 Nov 2026 10:00:30 GMT"}),
     30_000),
    (stub_errors.ServiceUnavailableError(GEMINI_RETRY_INFO), 37_000),
    (stub_errors.RateLimitError("x", headers={"retry-after": "soon"}), None),
    (stub_errors.RateLimitError("x", headers={"retry-after": "0"}), None),
    (stub_errors.RateLimitError("x"), None),
])  # fmt: skip
def test_retry_after_is_read_from_headers_or_a_gemini_retry_delay(
    raised: Exception, wait: int | None
) -> None:
    now = datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC)
    assert lite.retry_after_ms(raised, now=now) == wait
    got = lite.failure(raised)
    assert isinstance(got, TransientModelError)


# --- the fast classes, adapted -----------------------------------------------------------

PAGE = "Acme sells the S1 sensor for EUR 279 per year. Support is by email."
STATE = (CitationSpan("p0", "o", "inputs", "r", "v", 0, len(PAGE), PAGE, "t"),)
KIND = QuestionSpec("kind", "Which kind of record?", options=("offer", "review", "other"))
PAID = QuestionSpec("paid", "Is a price stated?", type="boolean")


def test_decide_asks_for_a_schema_with_an_enum_of_options_and_a_probability_each(
    lib: StubLiteLLM,
) -> None:
    answer = {"answers": {
        "kind": {"choice": "offer", "probabilities": {"offer": 0.9, "review": 0.07, "other": 0.03}},
        "paid": {"choice": "false", "probabilities": {"true": 0.8, "false": 0.2}},
    }}  # fmt: skip
    lib.answers.append(json_message(answer, prompt=80, completion=20, cost=0.00005))
    model = LiteLLMClassificationModel("doc_kind", {**ENV, "AKSHA_LIVE_REASONING_EFFORT": "low"},
                                       lib)  # fmt: skip
    assert model.capabilities.native is False  # stated probabilities: adapted mode
    result = model.classify(ClassificationRequest("s", "doc_kind", "a record", (KIND, PAID)))
    got = dict(result.answers)
    assert got["kind"].choice == "offer" and got["kind"].probabilities_ppm[0] == ("offer", 900_000)
    assert got["paid"].choice is None  # its stated choice contradicts its distribution
    assert result.usage == Usage(80, 20, 50)
    sent = lib.calls[0]
    assert sent["reasoning_effort"] == "low" and sent["timeout"] == 90.0
    fmt = sent["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["name"] == "decide"
    props = fmt["json_schema"]["schema"]["properties"]["answers"]["properties"]
    assert props["kind"]["properties"]["choice"]["enum"] == ["offer", "review", "other"]
    assert props["kind"]["properties"]["probabilities"]["required"] == ["offer", "review", "other"]
    assert props["paid"]["properties"]["choice"]["enum"] == ["true", "false"]


def test_decide_samples_add_their_usage_and_vote(lib: StubLiteLLM) -> None:
    offer = {
        "answers": {
            "kind": {
                "choice": "offer",
                "probabilities": {"offer": 0.6, "review": 0.3, "other": 0.1},
            }
        }
    }
    review = {
        "answers": {
            "kind": {
                "choice": "review",
                "probabilities": {"offer": 0.2, "review": 0.7, "other": 0.1},
            }
        }
    }
    lib.answers += [json_message(offer), json_message(offer), json_message(review)]
    model = LiteLLMClassificationModel("doc_kind", {**ENV, "AKSHA_LIVE_SAMPLES": "3"}, lib)
    result = model.classify(ClassificationRequest("s", "doc_kind", "a record", (KIND,)))
    (answer,) = [a for _, a in result.answers]
    assert answer.choice == "offer" and result.usage == Usage(33, 21)


def test_extract_asks_for_the_typed_schema_and_keeps_only_verbatim_quotes(
    lib: StubLiteLLM,
) -> None:
    answer = {"fields": {
        "price": {"value": "279", "quotes": [{"passage": "p0", "quote": "EUR 279 per year"}],
                  "confidence": 0.95, "absent": 0.01},
        "channels": {"value": ["email", "phone"], "quotes": [
            {"passage": "p0", "quote": "by email"}, {"passage": "p0", "quote": "by phone"}],
            "confidence": 0.8, "absent": 0.0},
        "maker": {"value": "Acme Holdings", "quotes": [
            {"passage": "p0", "quote": "Acme Holdings"}], "confidence": 0.9, "absent": 0.0},
    }}  # fmt: skip
    lib.answers.append(json_message(answer))
    model = LiteLLMExtractionModel("page_facts", ENV, lib)
    assert model.capabilities.native is False and model.capabilities.returns_spans
    fields = (FieldSpec("price", "number"), FieldSpec("channels", "list"), FieldSpec("maker"))
    result = model.extract(ExtractionRequest("s", "page_facts", STATE, fields))
    got = dict(result.answers)
    (span,) = got["price"].spans
    assert PAGE[span.start : span.end] == "EUR 279 per year" and got["price"].value == "279"
    assert got["channels"].value == ("by email",)  # the invented item is dropped
    assert got["maker"].spans == ()  # a made-up quote locates nowhere, so code abstains
    assert result.usage == Usage(11, 7)
    schema = lib.calls[0]["response_format"]["json_schema"]["schema"]
    props = schema["properties"]["fields"]["properties"]
    assert props["channels"]["properties"]["value"]["type"] == ["array", "null"]
    quote = props["price"]["properties"]["quotes"]["items"]["properties"]
    assert quote["passage"]["enum"] == ["p0"]
    assert json.loads(lib.calls[0]["messages"][1]["content"])["passages"][0]["id"] == "p0"


def test_an_answer_that_is_not_json_is_refused(lib: StubLiteLLM) -> None:
    lib.answers.append(message("I think it is an offer."))
    with pytest.raises(PermanentModelError) as e:
        LiteLLMClassificationModel("doc_kind", ENV, lib).classify(
            ClassificationRequest("s", "doc_kind", "a record", (KIND,))
        )
    assert e.value.code == "live_refused"


# --- native file input: off by default ---------------------------------------------------

PDF = b"%PDF-1.4\n% synthetic test document\n%%EOF\n"
WITH_PDF = GenerationRequest(
    str(STEP), "generate",
    (Message("user", "Read the attached terms.",
             files=(FilePart("application/pdf", PDF, "SYN-terms.pdf"),)),),
)  # fmt: skip


def test_native_files_are_off_by_default_and_a_file_is_refused_before_any_call(
    lib: StubLiteLLM,
) -> None:
    with pytest.raises(PermanentModelError) as e:
        LiteLLMGenerationModel("generate", ENV, lib).generate(WITH_PDF)
    assert e.value.code == "native_files_off" and lib.calls == []
    with pytest.raises(PermanentModelError):
        LiteLLMGenerationModel("generate", {**ENV, "AKSHA_NATIVE_FILES": "yes"}, lib)


def test_with_the_opt_in_a_pdf_goes_as_a_file_part(lib: StubLiteLLM) -> None:
    lib.answers.append(message("The terms renew yearly."))
    model = LiteLLMGenerationModel("generate", {**ENV, "AKSHA_NATIVE_FILES": "on"}, lib)
    assert "application/pdf" in model.capabilities.file_types
    assert model.generate(WITH_PDF).text == "The terms renew yearly."
    text, part = lib.calls[0]["messages"][0]["content"]
    assert text == {"type": "text", "text": "Read the attached terms."}
    assert part["type"] == "file" and part["file"]["filename"] == "SYN-terms.pdf"
    prefix = "data:application/pdf;base64,"
    assert base64.b64decode(part["file"]["file_data"].removeprefix(prefix)) == PDF
    assert "%PDF" not in json.dumps(WITH_PDF.as_json())  # the record holds the hash only


def test_an_oversized_or_undeclared_file_is_refused_even_with_the_opt_in(
    lib: StubLiteLLM,
) -> None:
    model = LiteLLMGenerationModel("generate", {**ENV, "AKSHA_NATIVE_FILES": "on"}, lib)
    for part in (FilePart("application/pdf", b"0" * (MAX_INLINE_BYTES + 1)),
                 FilePart("application/zip", b"PK")):  # fmt: skip
        request = GenerationRequest(str(STEP), "generate", (Message("user", "x", files=(part,)),))
        with pytest.raises(PermanentModelError) as e:
            model.generate(request)
        assert e.value.code == "native_file_refused"
    assert lib.calls == []


# --- loading LiteLLM ---------------------------------------------------------------------


def test_loading_litellm_keeps_it_offline_and_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    lib = StubLiteLLM()
    monkeypatch.setitem(sys.modules, "litellm", lib)
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP", raising=False)
    assert lite.load_litellm() is lib
    # Without this LiteLLM downloads its price list from the network on import.
    import os

    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert lib.telemetry is False and lib.suppress_debug_info is True


def test_litellm_not_installed_is_a_clear_configuration_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "litellm", None)  # import litellm raises ImportError
    with pytest.raises(PermanentModelError) as e:
        lite.load_litellm()
    assert e.value.code == "live_config_missing" and "aksha-harness[live]" in e.value.message
    with pytest.raises(PermanentModelError):
        LiteLLMGenerationModel("generate", ENV).generate(REQUEST)


def test_missing_keys_names_the_variables_never_values() -> None:
    lib = StubLiteLLM({MODEL: ["GEMINI_API_KEY"]})
    assert lite.missing_keys(MODEL, lib) == ["GEMINI_API_KEY"]
    assert lite.missing_keys("ollama/small", lib) == []


def test_the_litellm_adapter_module_is_never_imported_offline() -> None:
    script = """
import sys
import aksha.app
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.rules import RuleGenerationModel
bind_aliases((), "fake", {"generate": RuleGenerationModel()})
assert "aksha.adapters.models.litellm" not in sys.modules, "the adapter was imported"
assert "litellm" not in sys.modules, "LiteLLM was imported"
"""
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
