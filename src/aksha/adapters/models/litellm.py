"""Live adapters over LiteLLM, one per model class: any provider LiteLLM reaches (ADR-P07 G6).

``live`` mode only, and the default live provider (``AKSHA_LIVE_PROVIDER=litellm``). No
offline run imports this module, and it imports LiteLLM only when an adapter is built, so
LiteLLM is the optional extra ``aksha-harness[live]``. Aksha writes no adapter per
provider: LiteLLM's ``completion`` speaks the chat-completions shape to every provider it
supports and maps it onto each provider's own API (Google Gemini, Anthropic, OpenAI,
Mistral, a local Ollama or vLLM server, and others).

Models are named in LiteLLM's form, ``<provider>/<model>`` (``gemini/gemini-4-neon``):
``AKSHA_LIVE_MODEL_<ALIAS>``, then ``AKSHA_LIVE_MODEL_<CLASS>`` (``GENERATION``,
``EXTRACTION``, ``CLASSIFICATION``), then ``AKSHA_LIVE_MODEL``. **Keys are never Aksha
settings**: LiteLLM reads each provider's standard variable from the environment
(``GEMINI_API_KEY``, ``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ...), and Aksha passes no
key, records none and puts none in a message. The binder asks LiteLLM which variables a
model needs (:func:`missing_keys`) and refuses the start ``live_key_missing`` naming them.

Settings, each read per alias, then per class, then for all (:func:`live_setting`):

``AKSHA_LIVE_TIMEOUT_S``           one attempt's timeout, in seconds; default 240 for
                                   generation and 90 for the fast classes (thinking models
                                   take minutes on a long context)
``AKSHA_LIVE_REASONING_EFFORT``    passed to LiteLLM as ``reasoning_effort`` (``minimal``,
                                   ``low``, ``medium``, ``high``, ``xhigh``; ``none`` to
                                   switch it off),
                                   which it maps to the provider's thinking setting; unset
                                   leaves the provider's default
``AKSHA_LIVE_BASE_URL``            passed as ``api_base`` (a local server); unset, the
                                   provider's own endpoint
``AKSHA_LIVE_SAMPLES``             classification only: ask N times and vote
``AKSHA_NATIVE_FILES``             ``on`` lets a message's file parts go to the model; off
                                   by default

What each class sends:

* **Generation.** Messages and tools in the chat-completions shape
  (:func:`aksha.adapters.models.live.to_wire`): each ``ToolSpec`` is a ``function`` tool
  with its JSON Schema as ``parameters``, a forced tool is ``tool_choice``, and the answer's
  ``tool_calls`` come back as :class:`ToolCall`. A call's ID is whatever the provider and
  LiteLLM wrote, and it is recorded and sent back as is, so state a provider keeps in the
  ID (LiteLLM carries Gemini's thought signature there) survives a restart.
* **Classification (decide).** ``response_format`` with a JSON schema: per question, a
  ``choice`` from an ``enum`` of its options and a probability per option. Stated
  probabilities are not calibrated log probabilities, so the adapter is recorded
  ``native=False`` (adapted mode, judged by each question's adapted thresholds), and an
  answer whose stated choice is not the top of its own distribution abstains.
* **Extraction.** ``response_format`` with the typed schema built from the request's
  fields, each value with verbatim quotes naming a passage from an ``enum`` of passage IDs.
  Code locates each quote (:func:`aksha.adapters.models.live.locate`) and grounding verifies
  it verbatim at save, as for every adapted extractor. Recorded ``native=False``.

Usage: LiteLLM's ``prompt_tokens`` is input and ``completion_tokens`` output (thinking
included, as providers bill it); its cost estimate, when it has a price for the model, is
``cost_micro_usd``. All three land on the step's ``usage``, so token and money budgets and
the measurements count real numbers.

Errors. LiteLLM's own retries are off (``num_retries=0``): every retry is the model step's,
so each attempt is a ``step.failed`` with what the provider said (``detail``: LiteLLM's
error class, the HTTP status, the provider and a short message with anything key-shaped
removed). A rate limit (429), a 5xx, a timeout and a lost connection are
``TransientModelError``; a context-window refusal is ``ContextOverflow``; 401 and 403 are
``live_unauthorised``; a content-policy block is ``live_blocked``; any other refusal is
``live_refused``; LiteLLM not installed is ``live_config_missing``.

**Native file input is off by default** (the maintainer's decision): every file goes
through intake's parsers and the decide-then-extract path, so one general model exercises
every path. The capability is declared (``file_types``, ``max_inline_bytes``) and the route
is built: with ``AKSHA_NATIVE_FILES=on`` a message's :class:`FilePart` goes as a
chat-completions ``file`` or ``image_url`` part (base64 data URL), which LiteLLM maps to
the provider's inline file input; whether a given model accepts a type is the provider's
answer. With it off, a request carrying a file is refused ``native_files_off`` before any
call.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, ClassVar, Literal

from aksha.adapters.models.live import (
    CLASSIFY_SYSTEM,
    EXTRACT_SYSTEM,
    LIVE_CONFIG_MISSING,
    LIVE_REFUSED,
    LIVE_UNAUTHORISED,
    class_answers,
    field_answers,
    from_wire,
    json_fraction,
    stated_probabilities,
    to_wire,
)
from aksha.runtime.models.errors import ContextOverflow, PermanentModelError, TransientModelError
from aksha.runtime.models.routing import live_model_name
from aksha.runtime.models.types import (
    FALSE,
    FIELD_TYPES,
    TRUE,
    ClassificationRequest,
    ClassificationResult,
    ExtractionRequest,
    ExtractionResult,
    FieldSpec,
    GenerationRequest,
    GenerationResult,
    Message,
    ModelCapabilities,
    QuestionSpec,
    Usage,
)

LITELLM_PIN = "1.111.0"
"""The LiteLLM release the extra pins (pyproject ``live``; the reason is recorded there)."""

LIVE_BLOCKED = "live_blocked"
NATIVE_FILES_OFF = "native_files_off"
NATIVE_FILE_REFUSED = "native_file_refused"

GENERATION_TIMEOUT_S = 240.0
FAST_TIMEOUT_S = 90.0
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})

FILE_TYPES: frozenset[str] = frozenset(
    {"application/pdf", "image/png", "image/jpeg", "image/webp", "image/gif"}
)
"""What the adapter can send natively (only with ``AKSHA_NATIVE_FILES=on``)."""

MAX_INLINE_BYTES = 20 * 1024 * 1024
"""The most raw file bytes one request carries inline: a conservative bound under the
common providers' request size limits (base64 adds a third)."""

_TRANSIENT_TYPES = frozenset(
    {"RateLimitError", "InternalServerError", "ServiceUnavailableError", "BadGatewayError",
     "APIConnectionError", "Timeout", "APITimeoutError"}
)  # fmt: skip
_UNAUTHORISED_TYPES = frozenset({"AuthenticationError", "PermissionDeniedError"})


def load_litellm() -> Any:
    """Import LiteLLM for live mode, offline-safe: no remote price list, no telemetry.

    LiteLLM fetches its model price map from the network on import unless
    ``LITELLM_LOCAL_MODEL_COST_MAP`` is set, so it is set (if unset) before the import and
    the bundled map is used. Not installed is ``live_config_missing``.
    """
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    try:
        import litellm
    except ImportError as e:
        message = "LiteLLM is not installed: pip install 'aksha-harness[live]'"
        raise PermanentModelError(LIVE_CONFIG_MISSING, message) from e
    litellm.telemetry = False
    litellm.suppress_debug_info = True
    return litellm


def missing_keys(model: str, module: Any | None = None) -> list[str]:
    """The environment variables LiteLLM says ``model`` needs and does not find (names only)."""
    lib = module if module is not None else load_litellm()
    try:
        found = lib.validate_environment(model=model)
    except Exception:  # an unknown provider: the call itself will say what is wrong
        return []
    missing = found.get("missing_keys") if isinstance(found, Mapping) else None
    return sorted(str(k) for k in missing or ())


def _fold(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).upper()


def live_setting(name: str, alias: str, model_class: str, env: Mapping[str, str]) -> str:
    """``AKSHA_LIVE_<NAME>_<ALIAS>``, then ``_<CLASS>``, then ``AKSHA_LIVE_<NAME>``."""
    base = f"AKSHA_LIVE_{name}"
    return (
        env.get(f"{base}_{_fold(alias)}")
        or env.get(f"{base}_{model_class.upper()}")
        or env.get(base, "")
    ).strip()


def native_files_on(env: Mapping[str, str]) -> bool:
    """``AKSHA_NATIVE_FILES``: ``on`` or ``off`` (the default); anything else is refused."""
    value = (env.get("AKSHA_NATIVE_FILES") or "off").strip().lower()
    if value not in ("on", "off"):
        raise PermanentModelError(LIVE_CONFIG_MISSING, "AKSHA_NATIVE_FILES is on or off")
    return value == "on"


def timeout_s(alias: str, model_class: str, env: Mapping[str, str]) -> float:
    """One attempt's timeout for ``alias`` (module doc); malformed or not positive is refused."""
    raw = live_setting("TIMEOUT_S", alias, model_class, env)
    if not raw:
        return GENERATION_TIMEOUT_S if model_class == "generation" else FAST_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not value > 0:
        raise PermanentModelError(LIVE_CONFIG_MISSING, "AKSHA_LIVE_TIMEOUT_S is seconds above 0")
    return value


def reasoning_effort(alias: str, model_class: str, env: Mapping[str, str]) -> str | None:
    """The ``reasoning_effort`` for ``alias``, or None to leave the provider's default."""
    raw = live_setting("REASONING_EFFORT", alias, model_class, env).lower()
    if not raw:
        return None
    if raw not in REASONING_EFFORTS:
        message = f"AKSHA_LIVE_REASONING_EFFORT is one of {sorted(REASONING_EFFORTS)}"
        raise PermanentModelError(LIVE_CONFIG_MISSING, message)
    return raw


# --- what is sent ------------------------------------------------------------------------


def _file_part(media_type: str, data: bytes, name: str) -> dict[str, Any]:
    url = f"data:{media_type};base64," + base64.b64encode(data).decode("ascii")
    if media_type.startswith("image/"):
        return {"type": "image_url", "image_url": {"url": url}}
    part: dict[str, Any] = {"type": "file", "file": {"file_data": url}}
    if name:
        part["file"]["filename"] = name
    return part


def with_files(messages: list[dict[str, Any]], source: tuple[Message, ...], on: bool) -> None:
    """Put each message's file parts into its wire form (module doc); refused when off."""
    carried = [m for m in source if m.files]
    if not carried:
        return
    if not on:
        message = "a message carries a file and AKSHA_NATIVE_FILES is off"
        raise PermanentModelError(NATIVE_FILES_OFF, message)
    total = sum(len(f.data) for m in carried for f in m.files)
    if total > MAX_INLINE_BYTES:
        message = f"{total} file bytes are over the inline limit of {MAX_INLINE_BYTES}"
        raise PermanentModelError(NATIVE_FILE_REFUSED, message)
    for wire, m in zip(messages, source, strict=True):
        if not m.files:
            continue
        parts: list[dict[str, Any]] = [{"type": "text", "text": m.text}] if m.text else []
        for f in m.files:
            if f.media_type not in FILE_TYPES:
                message = f"{f.media_type} is not a type the adapter sends natively"
                raise PermanentModelError(NATIVE_FILE_REFUSED, message)
            parts.append(_file_part(f.media_type, f.data, f.name))
        wire["content"] = parts


def json_schema_format(name: str, schema: Mapping[str, Any]) -> dict[str, Any]:
    """A ``response_format`` asking for JSON that fits ``schema`` (LiteLLM maps it per provider)."""
    return {"type": "json_schema", "json_schema": {"name": name, "schema": dict(schema)}}


def decide_schema(request: ClassificationRequest) -> dict[str, Any]:
    """The forced schema of a decide call: per question a ``choice`` enum and probabilities."""
    questions: dict[str, Any] = {}
    for q in request.questions:
        options = _options(q)
        questions[q.name] = {
            "type": "object",
            "properties": {
                "choice": {"type": "string", "enum": options},
                "probabilities": {
                    "type": "object",
                    "properties": {o: {"type": "number", "minimum": 0, "maximum": 1}
                                   for o in options},
                    "required": options,
                },
            },
            "required": ["choice", "probabilities"],
        }  # fmt: skip
    answers = {"type": "object", "properties": questions, "required": list(questions)}
    return {"type": "object", "properties": {"answers": answers}, "required": ["answers"]}


def _field_value_schema(f: FieldSpec) -> dict[str, Any]:
    if f.type == "list":
        return {"type": ["array", "null"], "items": {"type": "string"}}
    return {"type": ["string", "null"], "description": f"{f.type}: {f.description}".strip(": ")}


def extract_schema(request: ExtractionRequest) -> dict[str, Any]:
    """The forced schema of an extract call: each typed field with quotes naming a passage."""
    passages = [s.span_id for s in request.state] or [""]
    quote = {"type": "object", "properties": {
        "passage": {"type": "string", "enum": passages}, "quote": {"type": "string"}},
        "required": ["passage", "quote"]}  # fmt: skip
    fields = {
        f.name: {
            "type": "object",
            "properties": {
                "value": _field_value_schema(f),
                "quotes": {"type": "array", "items": quote},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "absent": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["value", "quotes", "confidence", "absent"],
        }
        for f in request.fields
    }
    body = {"type": "object", "properties": fields, "required": list(fields)}
    return {"type": "object", "properties": {"fields": body}, "required": ["fields"]}


# --- what comes back ---------------------------------------------------------------------


def response_data(response: Any) -> dict[str, Any]:
    """A LiteLLM ``ModelResponse`` (or a mapping) as plain JSON data."""
    if isinstance(response, Mapping):
        return dict(response)
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        data = dump()
        if isinstance(data, Mapping):
            return dict(data)
    raise PermanentModelError(LIVE_REFUSED, "LiteLLM's answer is not a response object")


def cost_micro_usd(response: Any) -> int:
    """LiteLLM's cost estimate for the call, in micro-US-dollars; 0 when it has no price."""
    hidden = getattr(response, "_hidden_params", None)
    raw = hidden.get("response_cost") if isinstance(hidden, Mapping) else None
    if raw is None or isinstance(raw, bool):
        return 0
    try:
        cost = Decimal(str(raw))
    except ArithmeticError:
        return 0
    return max(0, int((cost * 1_000_000).to_integral_value()))


def usage_of(data: Mapping[str, Any], cost: int = 0) -> Usage:
    """``usage`` as a :class:`Usage`: prompt tokens in, completion tokens (thinking too) out."""
    usage = data.get("usage") or {}
    if not isinstance(usage, Mapping):
        return Usage(cost_micro_usd=cost)
    return Usage(
        int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0), cost
    )


def _text_json(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """The JSON object a forced-schema answer holds; ``live_refused`` when it holds none."""
    choices = data.get("choices") or [{}]
    first = choices[0] if isinstance(choices[0], Mapping) else {}
    msg = first.get("message") or {}
    text = str((msg.get("content") if isinstance(msg, Mapping) else None) or "").strip()
    if text.startswith("```"):  # a provider that wraps JSON in a fence
        text = text.strip("`").removeprefix("json").strip()
    try:
        parsed = json.loads(text, parse_float=json_fraction)
    except json.JSONDecodeError as e:
        raise PermanentModelError(LIVE_REFUSED, "the answer's JSON does not parse") from e
    if not isinstance(parsed, Mapping):
        raise PermanentModelError(LIVE_REFUSED, "the answer is not a JSON object")
    return parsed


# --- errors ------------------------------------------------------------------------------

_KEYISH = re.compile(
    r"(AIza[0-9A-Za-z_\-]{20,}|sk-[A-Za-z0-9_\-]{12,}|(?i:bearer)\s+\S+|"
    r"(?i:key)=[^&\s\"']+|[A-Za-z0-9_\-]{40,})"
)
_PROVIDER_MESSAGE = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"')


def scrub(text: str, environ: Mapping[str, str] | None = None) -> str:
    """``text`` with every key-shaped run, and each ``*_KEY``/``*_TOKEN`` value set, removed."""
    env = os.environ if environ is None else environ
    for name, value in env.items():
        if len(value) >= 8 and name.upper().endswith(("_KEY", "_TOKEN", "_SECRET")):
            text = text.replace(value, "[redacted]")
    return _KEYISH.sub("[redacted]", text)


def short_message(error: BaseException, environ: Mapping[str, str] | None = None) -> str:
    """The provider's own message inside a LiteLLM error, scrubbed and cut to one line."""
    raw = str(getattr(error, "message", None) or error)
    found = _PROVIDER_MESSAGE.search(raw)
    text = found.group(1) if found else raw
    text = " ".join(scrub(text, environ).split())
    return text[:200]


def failure(error: BaseException, environ: Mapping[str, str] | None = None) -> Exception:
    """The Aksha model error for a LiteLLM exception (module doc, errors), with its detail."""
    kind = type(error).__name__
    status = getattr(error, "status_code", None)
    status = status if isinstance(status, int) and not isinstance(status, bool) else None
    provider = getattr(error, "llm_provider", None)
    text = short_message(error, environ)
    detail: dict[str, str | int] = {"type": kind, "message": text or kind}
    if status is not None:
        detail["status"] = status
    if isinstance(provider, str) and provider:
        detail["provider"] = provider
    said = f"{kind}{f' {status}' if status else ''}: {text}".rstrip(": ")
    if kind == "ContextWindowExceededError":
        return ContextOverflow(message=said, detail=detail)
    if kind in _UNAUTHORISED_TYPES or status in (401, 403):
        return PermanentModelError(LIVE_UNAUTHORISED, said, detail=detail)
    if kind in _TRANSIENT_TYPES or status in (408, 429) or (status or 0) >= 500:
        return TransientModelError(message=said, detail=detail)
    if kind == "ContentPolicyViolationError":
        return PermanentModelError(LIVE_BLOCKED, said, detail=detail)
    return PermanentModelError(LIVE_REFUSED, said, detail=detail)


# --- the adapters ------------------------------------------------------------------------


class _LiteLLMModel:
    """The settings and the LiteLLM call every class shares (module doc)."""

    model_class: ClassVar[str]

    def __init__(self, alias: str, env: Mapping[str, str], litellm: Any | None = None) -> None:
        """Read the settings from ``env`` (no key is among them); ``litellm`` is for tests."""
        self.alias = alias
        self._model = live_model_name(alias, self.model_class, env)
        self._timeout_s = timeout_s(alias, self.model_class, env)
        self._effort = reasoning_effort(alias, self.model_class, env)
        self._base = (env.get("AKSHA_LIVE_BASE_URL") or "").strip() or None
        self._files = native_files_on(env)
        self._lib = litellm

    def __repr__(self) -> str:
        """The class, alias and model only."""
        return f"{type(self).__name__}(alias={self.alias!r}, model={self._model!r})"

    def _complete(self, **wire: Any) -> tuple[dict[str, Any], int]:
        """One ``litellm.completion`` call: the answer as data and its cost in micro-USD."""
        if not self._model:
            message = f"set AKSHA_LIVE_MODEL or AKSHA_LIVE_MODEL_{self.model_class.upper()}"
            raise PermanentModelError(LIVE_CONFIG_MISSING, message)
        if self._lib is None:
            self._lib = load_litellm()
        kwargs: dict[str, Any] = {"model": self._model, "timeout": self._timeout_s,
                                  "num_retries": 0, **wire}  # fmt: skip
        if self._effort is not None:
            kwargs["reasoning_effort"] = self._effort
        if self._base is not None:
            kwargs["api_base"] = self._base
        try:
            response = self._lib.completion(**kwargs)
        except Exception as e:
            raise failure(e) from None
        return response_data(response), cost_micro_usd(response)

    def _json_answer(
        self, system: str, ask: Mapping[str, Any], name: str, schema: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], Usage]:
        """One forced-schema call: the parsed object and the call's usage."""
        data, cost = self._complete(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(ask, ensure_ascii=False)},
            ],
            response_format=json_schema_format(name, schema),
        )
        return _text_json(data), usage_of(data, cost)


class LiteLLMGenerationModel(_LiteLLMModel):
    """Generation through LiteLLM: messages and tools in, text and tool calls out."""

    model_class: ClassVar[Literal["generation"]] = "generation"
    capabilities: ClassVar[ModelCapabilities] = ModelCapabilities(
        native=True, offline=False, file_types=FILE_TYPES, max_inline_bytes=MAX_INLINE_BYTES
    )

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """One ``completion`` call (module doc, generation)."""
        wire = to_wire(request, self._model)
        wire.pop("model")
        with_files(wire["messages"], request.messages, self._files)
        data, cost = self._complete(**wire)
        result = from_wire(data)
        return GenerationResult(result.text, result.tool_calls, result.finish,
                                usage_of(data, cost))  # fmt: skip


class LiteLLMExtractionModel(_LiteLLMModel):
    """The extraction contract under a forced typed schema, adapted (module doc)."""

    model_class: ClassVar[Literal["extraction"]] = "extraction"
    capabilities: ClassVar[ModelCapabilities] = ModelCapabilities(
        native=False, field_types=frozenset(FIELD_TYPES), returns_spans=True, offline=False
    )

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """One call for every field of ``request``."""
        ask = {
            "passages": [{"id": s.span_id, "text": s.text} for s in request.state],
            "fields": [{"name": f.name, "type": f.type, "description": f.description}
                       for f in request.fields],
        }  # fmt: skip
        answer, usage = self._json_answer(EXTRACT_SYSTEM, ask, "extract", extract_schema(request))
        return ExtractionResult(field_answers(answer, request), usage)


def _options(q: QuestionSpec) -> list[str]:
    return [TRUE, FALSE] if q.type == "boolean" else list(q.options)


def consistent(answer: Mapping[str, Any], request: ClassificationRequest) -> dict[str, Any]:
    """The answers whose stated choice is the top of their own distribution; others dropped."""
    found = answer.get("answers")
    kept: dict[str, Any] = {}
    for q in request.questions:
        item = found.get(q.name) if isinstance(found, Mapping) else None
        stated = stated_probabilities(item, q)
        if not stated or not isinstance(item, Mapping):
            continue
        if stated.get(str(item.get("choice")), Decimal(-1)) == max(stated.values()):
            kept[q.name] = item
    return {"answers": kept}


class LiteLLMClassificationModel(_LiteLLMModel):
    """The decide contract under a forced schema, its distribution stated, adapted."""

    model_class: ClassVar[Literal["classification"]] = "classification"
    capabilities: ClassVar[ModelCapabilities] = ModelCapabilities(
        native=False,
        question_types=frozenset({"choice", "boolean", "score"}),
        returns_distribution=True,
        offline=False,
    )

    def __init__(self, alias: str, env: Mapping[str, str], litellm: Any | None = None) -> None:
        """As every LiteLLM class, plus the sample count (``AKSHA_LIVE_SAMPLES``)."""
        super().__init__(alias, env, litellm)
        raw = (env.get("AKSHA_LIVE_SAMPLES") or "1").strip()
        if not raw.isdigit() or int(raw) < 1:
            raise PermanentModelError(LIVE_CONFIG_MISSING, "AKSHA_LIVE_SAMPLES is a count above 0")
        self._samples = int(raw)

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """One call (or one per sample) for every question of ``request``."""
        ask = {
            "state": request.state,
            "questions": [{"name": q.name, "prompt": q.prompt, "type": q.type,
                           "options": _options(q)} for q in request.questions],
        }  # fmt: skip
        schema = decide_schema(request)
        rounds = []
        spent = Usage()
        for _ in range(self._samples):
            answer, u = self._json_answer(CLASSIFY_SYSTEM, ask, "decide", schema)
            spent = Usage(spent.input_tokens + u.input_tokens,
                          spent.output_tokens + u.output_tokens,
                          spent.cost_micro_usd + u.cost_micro_usd)  # fmt: skip
            rounds.append(consistent(answer, request))
        return ClassificationResult(class_answers(rounds, request), spent)
