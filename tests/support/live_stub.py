"""A chat-completions server on localhost for live-mode tests: no network, no real model.

``StubServer`` answers ``POST /v1/chat/completions`` the way a general model under a forced
JSON schema would (ADR-P07 G3, adapted mode), by asking an agent's offline fast-class fakes
and writing their answers in the wire shape the live adapters read:

* an extraction request (the built-in extraction adapter's system prompt) becomes an
  ``ExtractionRequest`` over the passages it carries; each answer's span is sent back as a
  verbatim ``quote`` of its passage, so the adapter locates it in code, as it would a real
  model's quote;
* a classification request becomes a ``ClassificationRequest``; the fake's distribution is
  sent back as stated probabilities.

The fake is chosen by the request's ``model``: ``stub:<alias>``, so the test also proves
the per-alias model setting (``AKSHA_LIVE_MODEL_<ALIAS>``) reaches the wire. Every request
is kept (``requests``) with its headers, so a test can check what was sent and that the
key travelled only in the ``Authorization`` header.

``OfflineGeneration`` is the generation adapter a live test names with
``AKSHA_ALIAS_GENERATE=tests.support.live_stub:OfflineGeneration``: the agent's rule-based
policy, which needs the step ID the wire does not carry, bound the way any named live
adapter is (``cls(alias, env)``).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

from aksha.adapters.models.live import CLASSIFY_SYSTEM, EXTRACT_SYSTEM
from aksha.runtime.grounding.types import CitationSpan
from aksha.runtime.models.types import (
    PPM,
    ClassificationRequest,
    ExtractionRequest,
    FieldSpec,
    GenerationRequest,
    GenerationResult,
    ModelCapabilities,
    QuestionSpec,
)


def _extract(fake: Any, body: Mapping[str, Any]) -> dict[str, Any]:
    ask = body["passages"], body["fields"]
    state = tuple(
        CitationSpan(p["id"], "o", "inputs", "r", "v", 0, len(p["text"]), p["text"], "t")
        for p in ask[0]
    )
    fields = tuple(FieldSpec(f["name"], f["type"], description=f["description"]) for f in ask[1])
    result = fake.extract(ExtractionRequest("live/stub", "stub", state, fields))
    texts = {s.span_id: s.text for s in state}
    out: dict[str, Any] = {}
    for name, answer in result.answers:
        quotes = [{"passage": r.span_id, "quote": texts[r.span_id][r.start : r.end]}
                  for r in answer.spans]  # fmt: skip
        value = list(answer.value) if isinstance(answer.value, tuple) else answer.value
        out[name] = {
            "value": value,
            "quotes": quotes,
            "confidence": (answer.confidence_ppm or 0) / PPM,
            "absent": (answer.p_null_ppm or 0) / PPM,
        }
    return {"fields": out}


def _classify(fake: Any, body: Mapping[str, Any]) -> dict[str, Any]:
    questions = tuple(
        QuestionSpec(q["name"], q["prompt"], q["type"],
                     tuple(q["options"]) if q["type"] != "boolean" else ())
        for q in body["questions"]
    )  # fmt: skip
    result = fake.classify(ClassificationRequest("live/stub", "stub", body["state"], questions))
    out: dict[str, Any] = {}
    for name, answer in result.answers:
        out[name] = {"probabilities": {k: v / PPM for k, v in answer.probabilities_ppm}}
    return {"answers": out}


class StubServer:
    """The localhost server (module doc); ``fakes`` maps an alias to its offline fake."""

    def __init__(self, fakes: Mapping[str, Any]) -> None:
        """Serve on a free port of 127.0.0.1 until :meth:`close`."""
        self.fakes = dict(fakes)
        self.requests: list[dict[str, Any]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size))
                stub.requests.append({"path": self.path, "headers": dict(self.headers),
                                      "body": body})  # fmt: skip
                model = str(body.get("model", ""))
                fake = stub.fakes.get(model.removeprefix("stub:"))
                system = body["messages"][0]["content"]
                ask = json.loads(body["messages"][1]["content"])
                if fake is None:
                    self._send(404, {"error": {"message": f"no model {model}"}})
                    return
                if system == EXTRACT_SYSTEM:
                    content = _extract(fake, ask)
                elif system == CLASSIFY_SYSTEM:
                    content = _classify(fake, ask)
                else:
                    self._send(400, {"error": {"message": "not a fast-class request"}})
                    return
                usage = {"prompt_tokens": 10, "completion_tokens": 5}
                answer = {"choices": [{"message": {"content": json.dumps(content)}}],
                          "usage": usage}  # fmt: skip
                self._send(200, answer)

            def _send(self, status: int, data: Mapping[str, Any]) -> None:
                raw = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        """The server's chat-completions base URL."""
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self) -> None:
        """Stop serving."""
        self.server.shutdown()
        self.server.server_close()


class OfflineGeneration:
    """The research agent's rule-based policy as a named live generation adapter (module doc)."""

    model_class: ClassVar[str] = "generation"
    capabilities: ClassVar[ModelCapabilities] = ModelCapabilities(native=True, offline=True)

    def __init__(self, alias: str, env: Mapping[str, str]) -> None:
        """Bound like any live adapter; reads nothing from ``env``."""
        from aksha.agents.research.fakes import ResearchModel

        self.alias = alias
        self._policy = ResearchModel()

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """The policy's next calls."""
        return self._policy.generate(request)


class NoCreditsGeneration(OfflineGeneration):
    """:class:`OfflineGeneration` behind an OpenAI account with no credits left, while
    ``AKSHA_LIVE_TEST_NO_CREDITS=on``: each call raises what the LiteLLM adapter makes of
    OpenAI's 429 ``insufficient_quota`` (:func:`aksha.adapters.models.litellm.failure` over
    the stubbed LiteLLM error). With the setting off (credits added), the policy answers.
    """

    def __init__(self, alias: str, env: Mapping[str, str]) -> None:
        """Bound like any live adapter; reads ``AKSHA_LIVE_TEST_NO_CREDITS``."""
        super().__init__(alias, env)
        self._no_credits = env.get("AKSHA_LIVE_TEST_NO_CREDITS") == "on"

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """The provider's refusal while there are no credits, else the policy's next calls."""
        if self._no_credits:
            from aksha.adapters.models.litellm import failure
            from tests.support.litellm_stub import RateLimitError

            raise failure(RateLimitError(NO_CREDITS, llm_provider="openai"))
        return super().generate(request)


NO_CREDITS = (
    "You have no credits remaining. Add credits to continue using the API at "
    "https://platform.openai.com/settings/organization/billing/."
)
"""OpenAI's answer to a call from an account whose prepaid credits are used up."""


class FakesLiteLLM:
    """The LiteLLM module answering every fast-class call from an agent's offline fakes.

    What :class:`StubServer` does over HTTP, as LiteLLM's ``completion`` (installed in
    ``sys.modules`` by the test): the fake is chosen by the call's ``model``
    (``stub:<alias>``). ``fail`` holds the numbers of the calls (from 1) that time out
    instead, as LiteLLM's ``Timeout``, so a test can make one call of a tool body fail
    after the calls before it were answered. (It once answered a classification asked
    before the first failure with its first option the first time, as a real model need not
    answer one request the same way twice; a run now asks each request once,
    ``models.calls.FastAnswers``, so only the failed request is asked again.)
    Every call's keyword arguments are kept.
    """

    def __init__(self, fakes: Mapping[str, Any], fail: frozenset[int] = frozenset()) -> None:
        """Answer from ``fakes`` (alias to fake); time out the calls numbered in ``fail``."""
        self.fakes = dict(fakes)
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        self.asked: set[str] = set()
        self.telemetry = True
        self.suppress_debug_info = False

    def completion(self, **kwargs: Any) -> Any:
        from tests.support.litellm_stub import StubResponse, Timeout

        self.calls.append(kwargs)
        if len(self.calls) in self.fail:
            raise Timeout("Request timed out.", llm_provider="openai")
        fake = self.fakes[str(kwargs["model"]).removeprefix("stub:")]
        system = kwargs["messages"][0]["content"]
        ask = json.loads(kwargs["messages"][1]["content"])
        content = _extract(fake, ask) if system == EXTRACT_SYSTEM else _classify(fake, ask)
        again = json.dumps(kwargs["messages"], sort_keys=True)
        if system != EXTRACT_SYSTEM:
            for q in ask["questions"]:
                options = list(q.get("options") or [])
                odds = content["answers"].get(q["name"], {}).get("probabilities", {})
                full = {o: float(odds.get(o, 0.0)) for o in options}
                choice = max(options, key=lambda o: full[o]) if options else None
                content["answers"][q["name"]] = {"choice": choice, "probabilities": full}
        self.asked.add(again)
        message = {"role": "assistant", "content": json.dumps(content)}
        return StubResponse({"choices": [{"index": 0, "finish_reason": "stop",
                                          "message": message}],
                             "usage": {"prompt_tokens": 10, "completion_tokens": 5}})  # fmt: skip

    def validate_environment(self, model: str) -> dict[str, Any]:
        """Every model has what it needs."""
        return {"keys_in_environment": True, "missing_keys": []}
