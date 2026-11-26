"""``LiveGenerationModel``: generation over a chat-completions HTTP server (``live`` mode only).

Not wired yet: the composition root accepts only ``AKSHA_MODE=fake``, so no released
command runs it, and no package extra installs it. It is never imported by an offline run:
the binder imports it only in ``live`` mode, when ``AKSHA_ALIAS_GENERATE`` (or another
alias's variable) names ``aksha.adapters.models.live:LiveGenerationModel`` (ADR-P07 D9).
It speaks the widely used ``POST <base>/chat/completions`` shape, so a hosted model and a
local server on ``localhost`` both fit. It uses only the standard library.

Configuration is read from the environment in ``__init__`` and held on the instance:
``AKSHA_LIVE_BASE_URL`` (required), ``AKSHA_LIVE_MODEL`` (required), ``AKSHA_LIVE_API_KEY``
(optional for a local server) and ``AKSHA_LIVE_TIMEOUT_S`` (default 60). The key is
never written to an event, an error message or a ``repr``; ``run.started`` records only
the adapter's class.

Errors: HTTP 429 and 5xx, and a connection failure or timeout, are
``TransientModelError`` (retried by the model step); a 400 that names the context length
is ``ContextOverflow``; 401 and 403 are ``live_unauthorised``; any other refusal is
``live_refused``; missing configuration is ``live_config_missing``.

At this edge every JSON number with a fraction in a tool call's arguments becomes a
decimal string (``0.5`` is ``"0.5"``), because the event store refuses floats (D4); an
integral number stays an integer.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any, ClassVar, Literal

from aksha.runtime.models.errors import ContextOverflow, PermanentModelError, TransientModelError
from aksha.runtime.models.fixed import decimal_string
from aksha.runtime.models.types import (
    FinishReason,
    GenerationRequest,
    GenerationResult,
    Message,
    ModelCapabilities,
    ToolCall,
    Usage,
)

Transport = Callable[[str, Mapping[str, str], bytes, float], tuple[int, bytes]]
"""``(url, headers, body, timeout_s) -> (status, body)``; tests pass one with no network."""

LIVE_CONFIG_MISSING = "live_config_missing"
LIVE_UNAUTHORISED = "live_unauthorised"
LIVE_REFUSED = "live_refused"

_FINISH: Mapping[str, FinishReason] = {
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "stop": "text",
    "length": "length",
    "content_filter": "refusal",
}


def urllib_post(
    url: str, headers: Mapping[str, str], body: bytes, timeout_s: float
) -> tuple[int, bytes]:
    """POST ``body`` with the standard library; an HTTP error status is returned, not raised."""
    req = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as e:
        return int(e.code), e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TransientModelError(message=f"no answer from the server ({type(e).__name__})") from e


def _message(m: Message) -> dict[str, Any]:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.call_id, "content": m.text or ""}
    out: dict[str, Any] = {"role": m.role, "content": m.text}
    if m.tool_calls:
        out["tool_calls"] = [
            {
                "id": c.call_id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(dict(c.arguments))},
            }
            for c in m.tool_calls
        ]
    return out


def to_wire(request: GenerationRequest, model: str) -> dict[str, Any]:
    """The chat-completions request body for ``request``."""
    body: dict[str, Any] = {
        "model": model,
        "messages": [_message(m) for m in request.messages],
    }
    if request.tools:
        request_json = request.as_json()
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["schema"],
                },
            }
            for t in request_json["tools"]
        ]
    if request.tool_choice is not None:
        body["tool_choice"] = {"type": "function", "function": {"name": request.tool_choice}}
    return body


def _fraction(text: str) -> str:
    """A JSON number with a fraction or exponent, as a decimal string (D4)."""
    return decimal_string(Decimal(text))


def _arguments(raw: object) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        raw = json.dumps(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw, parse_float=_fraction)
    except json.JSONDecodeError:
        return {"_unparsed": raw}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


def from_wire(data: Mapping[str, Any]) -> GenerationResult:
    """A chat-completions response body as a ``GenerationResult``."""
    choices = data.get("choices") or [{}]
    choice = choices[0] if isinstance(choices[0], Mapping) else {}
    msg = choice.get("message") or {}
    calls = tuple(
        ToolCall(
            str(c.get("id") or f"call_{i + 1}"),
            str((c.get("function") or {}).get("name", "")),
            _arguments((c.get("function") or {}).get("arguments")),
        )
        for i, c in enumerate(msg.get("tool_calls") or ())
        if isinstance(c, Mapping)
    )
    finish = _FINISH.get(str(choice.get("finish_reason")), "tool_calls" if calls else "text")
    usage = data.get("usage") or {}
    text = msg.get("content")
    return GenerationResult(
        None if text is None else str(text),
        calls,
        finish,
        Usage(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)),
    )


class LiveGenerationModel:
    """A generation adapter over a chat-completions server (module doc)."""

    model_class: ClassVar[Literal["generation"]] = "generation"
    capabilities: ClassVar[ModelCapabilities] = ModelCapabilities(native=True, offline=False)

    def __init__(
        self, alias: str, env: Mapping[str, str], transport: Transport | None = None
    ) -> None:
        """Read the configuration (and the key) from ``env``; ``transport`` defaults to urllib."""
        self.alias = alias
        self._base = env.get("AKSHA_LIVE_BASE_URL", "").rstrip("/")
        self._model = env.get("AKSHA_LIVE_MODEL", "")
        self.__key = env.get("AKSHA_LIVE_API_KEY") or None
        self._timeout_s = float(env.get("AKSHA_LIVE_TIMEOUT_S", "60"))
        self._transport = transport or urllib_post

    def __repr__(self) -> str:
        """The alias and model only; never the key."""
        return f"LiveGenerationModel(alias={self.alias!r}, model={self._model!r})"

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """One chat-completions call; statuses mapped as the module doc says."""
        if not self._base or not self._model:
            message = "set AKSHA_LIVE_BASE_URL and AKSHA_LIVE_MODEL"
            raise PermanentModelError(LIVE_CONFIG_MISSING, message)
        headers = {"Content-Type": "application/json"}
        if self.__key:
            headers["Authorization"] = "Bearer " + self.__key
        body = json.dumps(to_wire(request, self._model)).encode("utf-8")
        status, raw = self._transport(
            f"{self._base}/chat/completions", headers, body, self._timeout_s
        )
        if status == 429 or status >= 500:
            raise TransientModelError(message=f"the server answered {status}")
        if status in (401, 403):
            raise PermanentModelError(LIVE_UNAUTHORISED, f"the server answered {status}")
        if status >= 400:
            if status == 400 and b"context" in raw.lower():
                raise ContextOverflow(message="the request is over the context window")
            raise PermanentModelError(LIVE_REFUSED, f"the server answered {status}")
        try:
            data = json.loads(raw.decode("utf-8"), parse_float=_fraction)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise TransientModelError(message="the server's answer is not JSON") from e
        if not isinstance(data, Mapping):
            raise PermanentModelError(LIVE_REFUSED, "the server's answer is not an object")
        return from_wire(data)
