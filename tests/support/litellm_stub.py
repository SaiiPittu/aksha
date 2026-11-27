"""A stand-in for the LiteLLM module: the names the LiteLLM adapters use, and no network.

:class:`StubLiteLLM` has ``completion`` (records every call's keyword arguments and
answers from a queue), ``validate_environment`` (the variables a model needs, from a
table) and LiteLLM's error classes by name, each with ``status_code``, ``llm_provider``
and ``message`` as LiteLLM's own carry them. A queued answer is a response dictionary,
returned as an object with ``model_dump`` and ``_hidden_params`` (a LiteLLM
``ModelResponse`` has both), or an exception, which is raised. Every value is synthetic.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


class StubResponse:
    """What LiteLLM's ``completion`` returns, as far as the adapters read it."""

    def __init__(self, data: Mapping[str, Any], cost: float | None = None) -> None:
        self._data = dict(data)
        self._hidden_params = {"response_cost": cost, "custom_llm_provider": "stub"}

    def model_dump(self) -> dict[str, Any]:
        copied: dict[str, Any] = json.loads(json.dumps(self._data))
        return copied


class StubError(Exception):
    """A LiteLLM exception: its class name, status and provider are what the adapter reads."""

    status_code: int | None = None

    def __init__(self, message: str, *, llm_provider: str = "gemini", status: int | None = None):
        super().__init__(message)
        self.message = message
        self.llm_provider = llm_provider
        if status is not None:
            self.status_code = status


def _error(name: str, status: int | None) -> type[StubError]:
    return type(name, (StubError,), {"status_code": status})


RateLimitError = _error("RateLimitError", 429)
ServiceUnavailableError = _error("ServiceUnavailableError", 503)
InternalServerError = _error("InternalServerError", 500)
Timeout = _error("Timeout", 408)
APIConnectionError = _error("APIConnectionError", 500)
AuthenticationError = _error("AuthenticationError", 401)
ContextWindowExceededError = _error("ContextWindowExceededError", 400)
ContentPolicyViolationError = _error("ContentPolicyViolationError", 400)
BadRequestError = _error("BadRequestError", 400)
NotFoundError = _error("NotFoundError", 404)


class StubLiteLLM:
    """The LiteLLM module as the adapters see it (module doc)."""

    def __init__(self, needs: Mapping[str, list[str]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers: list[StubResponse | BaseException] = []
        self.needs = dict(needs or {})
        self.telemetry = True
        self.suppress_debug_info = False

    def completion(self, **kwargs: Any) -> StubResponse:
        self.calls.append(kwargs)
        if not self.answers:
            raise AssertionError("the stub has no answer queued")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def validate_environment(self, model: str) -> dict[str, Any]:
        missing = self.needs.get(model, [])
        return {"keys_in_environment": not missing, "missing_keys": list(missing)}


def message(
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    *,
    finish: str = "stop",
    prompt: int = 11,
    completion: int = 7,
    cost: float | None = None,
) -> StubResponse:
    """A chat-completions response as LiteLLM returns it."""
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    data = {
        "id": "SYN-resp-1",
        "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": finish, "message": msg}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                  "total_tokens": prompt + completion},
    }  # fmt: skip
    return StubResponse(data, cost)


def json_message(answer: Mapping[str, Any], **usage: Any) -> StubResponse:
    """A forced-schema answer: the JSON object as the message text."""
    return message(json.dumps(answer), **usage)
