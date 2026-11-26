"""The live adapter, with no network: a transport stands in for the server (ADR-P07 D9).

Also the offline guarantees: a fake-mode run opens no connection and needs no key, no
live module is imported in an offline mode, and a key set in the environment reaches no
table.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

import pytest

from aksha.adapters.models.live import LiveGenerationModel, from_wire, to_wire
from aksha.runtime.errors import AkshaError
from aksha.runtime.execution.types import StepId
from aksha.runtime.models.errors import ContextOverflow, PermanentModelError, TransientModelError
from aksha.runtime.models.routing import bind_aliases, live_adapters
from aksha.runtime.models.rules import RuleClassificationModel, RuleGenerationModel
from aksha.runtime.models.steps import generation_step, run_cascade
from aksha.runtime.models.types import (
    ClassificationRequest,
    GenerationRequest,
    GenerationResult,
    Message,
    ModelAlias,
    QuestionSpec,
    ToolCall,
    Usage,
)
from aksha.runtime.tools.types import ToolView
from tests.support.models import ModelHarness

KEY = "sk-test-0000-never-stored"
ENV = {
    "AKSHA_LIVE_BASE_URL": "http://model.invalid/v1/",
    "AKSHA_LIVE_MODEL": "small-model",
    "AKSHA_LIVE_API_KEY": KEY,
}
STEP = StepId("R-0001", "a", turn=1)
REQUEST = GenerationRequest(
    str(STEP), "generate",
    (Message("system", "Be brief."), Message("user", "Fetch acme."),
     Message("assistant", None, (ToolCall("c1", "fetch", {"id": "acme"}),)),
     Message("tool", '{"ok": true}', call_id="c1")),
    tools=(ToolView("fetch", "Fetch a page.", {"type": "object"}),),
    tool_choice="fetch",
)  # fmt: skip
ANSWER = {
    "choices": [{
        "message": {"content": None, "tool_calls": [{
            "id": "call_9", "type": "function",
            "function": {"name": "fetch", "arguments": '{"id": "globex", "weight": 0.5, "n": 2}'},
        }]},
        "finish_reason": "tool_calls",
    }],
    "usage": {"prompt_tokens": 40, "completion_tokens": 9},
}  # fmt: skip


class Server:
    """A transport that records what it was sent and answers from a list."""

    def __init__(self, *answers: tuple[int, Any]) -> None:
        self.answers = list(answers)
        self.sent: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> tuple[int, bytes]:
        self.sent.append((url, dict(headers), json.loads(body)))
        status, payload = self.answers.pop(0)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, raw


def test_the_request_and_the_answer_are_translated_at_the_edge() -> None:
    body = to_wire(REQUEST, "small-model")
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "tool"]
    assert body["messages"][2]["tool_calls"][0]["function"] == {
        "name": "fetch", "arguments": '{"id": "acme"}',
    }  # fmt: skip
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'}
    assert body["tools"][0]["function"]["name"] == "fetch"
    assert body["tool_choice"] == {"type": "function", "function": {"name": "fetch"}}
    result = from_wire(ANSWER)
    assert result == GenerationResult(
        None, (ToolCall("call_9", "fetch", {"id": "globex", "weight": "0.5", "n": 2}),),
        "tool_calls", Usage(40, 9, 0),
    )  # fmt: skip
    assert (
        from_wire({"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}).text
        == "hi"
    )


def test_a_live_call_sends_the_key_only_in_its_header(tmp_path: Path) -> None:
    server = Server((200, ANSWER))
    model = LiveGenerationModel("generate", ENV, transport=server)
    assert KEY not in repr(model) and KEY not in str(vars(model).get("_base"))
    h = ModelHarness(tmp_path / "a.sqlite")
    table = bind_aliases((), "live", {"generate": model})
    out = generation_step(h.executor(), STEP, table, REQUEST)
    assert out.result is not None and out.result.tool_calls[0].arguments["weight"] == "0.5"
    url, headers, _ = server.sent[0]
    assert url == "http://model.invalid/v1/chat/completions"
    assert headers["Authorization"] == "Bearer " + KEY
    assert table.run_started_fields()["models"][0]["adapter"] == (
        "aksha.adapters.models.live:LiveGenerationModel"
    )
    h.close()
    assert KEY.encode() not in (tmp_path / "a.sqlite").read_bytes()


@pytest.mark.parametrize(
    ("status", "body", "error", "code"),
    [
        (429, {}, TransientModelError, "model_transient"),
        (503, {}, TransientModelError, "model_transient"),
        (200, b"not json", TransientModelError, "model_transient"),
        (400, {"error": {"code": "context_length_exceeded"}}, ContextOverflow, "context_overflow"),
        (401, {}, PermanentModelError, "live_unauthorised"),
        (404, {}, PermanentModelError, "live_refused"),
        (200, [1, 2], PermanentModelError, "live_refused"),
    ],
)
def test_statuses_map_to_the_model_errors(
    status: int, body: Any, error: type[AkshaError], code: str
) -> None:
    model = LiveGenerationModel("generate", ENV, transport=Server((status, body)))
    with pytest.raises(error) as e:
        model.generate(REQUEST)
    assert e.value.code == code and KEY not in str(e.value)


def test_missing_configuration_is_refused_before_any_call() -> None:
    server = Server()
    with pytest.raises(PermanentModelError) as e:
        LiveGenerationModel("generate", {}, transport=server).generate(REQUEST)
    assert e.value.code == "live_config_missing" and server.sent == []


def test_the_binder_builds_the_named_live_adapter() -> None:
    env = {**ENV, "AKSHA_ALIAS_GENERATE": "aksha.adapters.models.live:LiveGenerationModel"}
    built = live_adapters((), env)
    assert isinstance(built["generate"], LiveGenerationModel)


# --- offline guarantees -----------------------------------------------------------------


class Moods(RuleClassificationModel):
    keywords: ClassVar = {"angry": {"true": ("furious",)}}


class Done(RuleGenerationModel):
    def generate(self, request: GenerationRequest) -> GenerationResult:
        return GenerationResult("done")


def test_fake_mode_needs_no_network_or_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a fake-mode run opened a connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setenv("AKSHA_LIVE_API_KEY", KEY)
    angry = QuestionSpec("angry", "Angry?", type="boolean")
    alias = ModelAlias("classify.mood", "classification", questions=(angry,))
    h = ModelHarness(tmp_path / "a.sqlite")
    table = bind_aliases((alias,), "fake", {"generate": Done(), "classify.mood": Moods()})
    assert generation_step(h.executor(), STEP, table, REQUEST).result == GenerationResult("done")
    call = StepId("R-0001", "a", turn=1, call=1)
    request = ClassificationRequest(str(call), "classify.mood", "Furious!", (angry,))
    assert run_cascade(h.executor(), call, table, request).accepted() == {"angry": True}
    h.close()
    assert KEY.encode() not in (tmp_path / "a.sqlite").read_bytes()


def test_live_adapter_never_imported_offline() -> None:
    script = """
import sys
import aksha.app
from aksha.adapters.models.replay import replay_adapters
from aksha.adapters.models.scripted import ScriptedGenerationModel
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.rules import RuleGenerationModel
bind_aliases((), "fake", {"generate": RuleGenerationModel()})
bind_aliases((), "scripted", {"generate": ScriptedGenerationModel([])})
bind_aliases((), "replay", replay_adapters((), {}))
assert "aksha.adapters.models.live" not in sys.modules, "live was imported"
assert "urllib.request" not in sys.modules, "an HTTP client was imported"
"""
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
