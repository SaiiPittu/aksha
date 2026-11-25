"""Model classes, aliases and the owned request and result types (ADR-P07).

Three model classes, each with its own contract:

1. **Extraction** (a fast class, System 1): fields in, typed values with source spans
   out, or null with a probability that the value is absent.
2. **Classification** (a fast class, System 1): questions in, one listed option or a
   boolean with a full probability distribution out.
3. **Generation** (the slow class, System 2): messages and tools in, text and tool calls
   out, with no reliable probability.

An agent declares model aliases, each bound to one class; a Task names the aliases it
uses, and every Task also has the implicit generation alias ``generate``. The run mode
binds each alias to one adapter of the same class (``routing.py``).

Every probability, ``p_null`` and confidence crossing the event boundary is a fixed-point
integer in parts per million (0 to 1,000,000), and every ``number`` value is a decimal
string (D4), because the event store refuses floats in hashed payloads. Thresholds are
written as fractions in the agent definition and converted once (``fixed.ppm``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from aksha.runtime.canonical import canonical_json, sha256_hex, thaw
from aksha.runtime.grounding.types import CitationSpan
from aksha.runtime.tasks.types import KeySource
from aksha.runtime.tools.types import ToolView

ModelClass = Literal["extraction", "classification", "generation"]
RunMode = Literal["fake", "scripted", "replay", "live"]
FieldType = Literal["text", "number", "date", "duration", "list"]
QuestionType = Literal["choice", "boolean", "score"]
OnExhausted = Literal["unknown", "review", "fail"]
FinishReason = Literal["tool_calls", "text", "length", "refusal"]
Role = Literal["system", "user", "assistant", "tool"]

MODEL_CLASSES: frozenset[str] = frozenset(get_args(ModelClass))
"""The three model classes."""

FAST_CLASSES: frozenset[str] = frozenset({"extraction", "classification"})
"""The thresholdable classes (System 1)."""

RUN_MODES: frozenset[str] = frozenset(get_args(RunMode))
"""``fake`` (the default), ``scripted``, ``replay`` and ``live``."""

OFFLINE_MODES: frozenset[str] = frozenset({"fake", "scripted", "replay"})
"""The modes that never reach a network."""

FIELD_TYPES: frozenset[str] = frozenset(get_args(FieldType))
QUESTION_TYPES: frozenset[str] = frozenset(get_args(QuestionType))
ON_EXHAUSTED: frozenset[str] = frozenset(get_args(OnExhausted))
FINISH_REASONS: frozenset[str] = frozenset(get_args(FinishReason))

IMPLICIT_GENERATION_ALIAS = "generate"
"""The generation alias every Task has without declaring it."""

PPM = 1_000_000
"""One whole, in parts per million: a probability of 1."""

TRUE, FALSE = "true", "false"
"""The two keys of a boolean question's distribution."""


@dataclass(frozen=True)
class ModelCapabilities:
    """The capability record of one adapter; routing branches on it, never on a name (D3).

    ``native`` is False for a generation model under a schema (an adapted adapter).
    """

    native: bool = True
    field_types: frozenset[str] = frozenset()
    question_types: frozenset[str] = frozenset()
    returns_spans: bool = False
    returns_distribution: bool = False
    offline: bool = True


@dataclass(frozen=True)
class ConfidenceThresholds:
    """The numbers code applies to a fast-class answer, as fractions of one (D4).

    ``accept_at``: an answer at or above it is accepted. ``reject_at``: for a boolean
    question, P(true) at or under it accepts False. ``null_at``: for extraction, a
    ``p_null`` at or over it accepts "absent". ``margin``: for a choice, the least gap
    between the top two options. Between accept and reject lies the abstention band.
    ``fitted_on`` is the hash of the labelled set that fitted them; None means unfitted,
    and every unfitted threshold is listed (``thresholds.unfitted_thresholds``).
    """

    accept_at: float = 0.9
    reject_at: float = 0.2
    null_at: float = 0.5
    margin: float | None = None
    fitted_on: str | None = None


@dataclass(frozen=True)
class FieldSpec:
    """One field an extraction alias fills. ``cited`` fields need a span in the state."""

    name: str
    type: FieldType = "text"
    thresholds: ConfidenceThresholds = ConfidenceThresholds()
    cited: bool = True
    description: str = ""


@dataclass(frozen=True)
class QuestionSpec:
    """One question a classification alias answers.

    Exactly one of ``options`` and ``options_from`` for a choice or score question (S1):
    static options, or the keys listed in a saved artifact's field, read when the request
    is built so the options are recorded data with lineage. A boolean question has
    neither. ``adapted`` holds the thresholds an adapted answer is judged by; None judges
    it by ``thresholds``.
    """

    name: str
    prompt: str
    type: QuestionType = "choice"
    options: tuple[str, ...] = ()
    options_from: KeySource | None = None
    thresholds: ConfidenceThresholds = ConfidenceThresholds()
    adapted: ConfidenceThresholds | None = None

    def thresholds_for(self, native: bool) -> ConfidenceThresholds:
        """The thresholds a native or an adapted answer is judged by."""
        return self.thresholds if native or self.adapted is None else self.adapted


@dataclass(frozen=True)
class ModelAlias:
    """A named use of a model class; namespaced by the agent at load (``ns.alias``).

    ``fallback`` names other aliases of the same class, tried in order when an answer
    abstains or a candidate fails. ``allow_adapted`` (classification only) lets an
    adapted classifier stand in, never for an alias that ``gates_effect``. ``fields``
    (extraction) and ``questions`` (classification) are what the alias answers; the loader
    and the binder check them against each adapter's capabilities.
    """

    name: str
    model_class: ModelClass
    fallback: tuple[str, ...] = ()
    allow_adapted: bool = False
    on_exhausted: OnExhausted | None = None
    fields: tuple[FieldSpec, ...] = ()
    questions: tuple[QuestionSpec, ...] = ()
    gates_effect: bool = False


@dataclass(frozen=True)
class ModelAdapterSpec:
    """A model adapter an extension contributes: its name, class and a factory for it.

    The factory takes no arguments and returns an adapter of ``model_class``.
    """

    name: str
    model_class: ModelClass
    factory: Callable[..., object]


# --- generation (the slow class) ------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """One tool call a generation model asked for; ``arguments`` is JSON data."""

    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        """The call as JSON data."""
        return {"call_id": self.call_id, "name": self.name, "arguments": thaw(self.arguments)}


@dataclass(frozen=True)
class Message:
    """One message of a generation context. A ``tool`` message answers ``call_id``."""

    role: Role
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    call_id: str | None = None

    def as_json(self) -> dict[str, Any]:
        """The message as JSON data."""
        return {
            "role": self.role,
            "text": self.text,
            "tool_calls": [c.as_json() for c in self.tool_calls],
            "call_id": self.call_id,
        }


@dataclass(frozen=True)
class Usage:
    """What one model call spent; the runtime records it on the model step, and only there."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_micro_usd: int = 0

    def as_json(self) -> dict[str, int]:
        """The three counts, as ``step.completed{usage}`` carries them."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_micro_usd": self.cost_micro_usd,
        }


@dataclass(frozen=True)
class GenerationRequest:
    """What a generation model is asked: the messages and the tools it is offered.

    ``tool_choice`` is a hint the forced-tool stop policy may set; fakes may ignore it.
    """

    step_id: str
    alias: str
    messages: tuple[Message, ...]
    tools: tuple[ToolView, ...] = ()
    tool_choice: str | None = None

    def as_json(self) -> dict[str, Any]:
        """What the model sees, as canonical JSON data: the hashed form of the context."""
        return {
            "alias": self.alias,
            "messages": [m.as_json() for m in self.messages],
            "tools": [
                {"name": t.name, "description": t.description, "schema": thaw(t.schema)}
                for t in self.tools
            ],
            "tool_choice": self.tool_choice,
        }

    @property
    def context_hash(self) -> str:
        """``sha256:`` over the canonical messages, tools and tool choice (the args hash)."""
        return "sha256:" + sha256_hex(self.as_json())

    @property
    def context_chars(self) -> int:
        """The length of the canonical context, in characters."""
        return len(canonical_json(self.as_json()))


@dataclass(frozen=True)
class GenerationResult:
    """What a generation model answered. ``finish`` says why it stopped."""

    text: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    finish: FinishReason = "text"
    usage: Usage = Usage()


# --- extraction and classification (the fast classes) ---------------------------------


@dataclass(frozen=True)
class SpanRef:
    """Code points ``[start, end)`` inside the text of one minted citation span."""

    span_id: str
    start: int
    end: int


FieldValue = str | tuple[str, ...] | None
"""An extracted value: a string (a ``number`` is a decimal string), a list, or None."""


@dataclass(frozen=True)
class FieldAnswer:
    """One field's answer. Every probability is an integer in parts per million (D4)."""

    value: FieldValue
    spans: tuple[SpanRef, ...] = ()
    p_null_ppm: int | None = None
    confidence_ppm: int | None = None


@dataclass(frozen=True)
class ExtractionRequest:
    """Fields to fill from minted citation spans (their canonical text, PR-13)."""

    step_id: str
    alias: str
    state: tuple[CitationSpan, ...]
    fields: tuple[FieldSpec, ...]

    def as_json(self) -> dict[str, Any]:
        """What the model sees: the alias, each span's ID and text, each field's name and type.

        Thresholds are left out: code applies them, so changing one re-judges a recorded
        answer instead of asking again.
        """
        return {
            "alias": self.alias,
            "state": [{"span_id": s.span_id, "text": s.text} for s in self.state],
            "fields": [
                {"name": f.name, "type": f.type, "cited": f.cited, "description": f.description}
                for f in self.fields
            ],
        }

    @property
    def request_hash(self) -> str:
        """``sha256:`` over :meth:`as_json`: the args hash of the step."""
        return "sha256:" + sha256_hex(self.as_json())


@dataclass(frozen=True)
class ExtractionResult:
    """One answer per field, by field name, and what the call spent."""

    answers: tuple[tuple[str, FieldAnswer], ...]
    usage: Usage = Usage()

    def answer(self, name: str) -> FieldAnswer | None:
        """The answer to field ``name``, or None when the adapter left it out."""
        return dict(self.answers).get(name)


Choice = str | bool | None
"""A classification answer: a listed option, a boolean, or None when the adapter refused."""


@dataclass(frozen=True)
class ClassAnswer:
    """One question's answer and its full distribution in parts per million (D4).

    ``probabilities_ppm`` is sorted by probability, highest first, then by option; a
    boolean question's options are ``"true"`` and ``"false"``.
    """

    choice: Choice
    probabilities_ppm: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class ClassificationRequest:
    """Questions about a state rendered by code, never by a model.

    ``state_inputs`` are the citation span IDs and artifact refs behind the state, and
    the artifact an ``options_from`` question read its options from (lineage). Every
    question carries its options resolved.
    """

    step_id: str
    alias: str
    state: str
    questions: tuple[QuestionSpec, ...]
    state_inputs: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        """What the model sees: the alias, the state, its inputs and each question's options."""
        return {
            "alias": self.alias,
            "state": self.state,
            "state_inputs": list(self.state_inputs),
            "questions": [
                {"name": q.name, "prompt": q.prompt, "type": q.type, "options": list(q.options)}
                for q in self.questions
            ],
        }

    @property
    def request_hash(self) -> str:
        """``sha256:`` over :meth:`as_json`: the args hash of the step."""
        return "sha256:" + sha256_hex(self.as_json())


@dataclass(frozen=True)
class ClassificationResult:
    """One answer per question, by question name, and what the call spent."""

    answers: tuple[tuple[str, ClassAnswer], ...]
    usage: Usage = Usage()

    def answer(self, name: str) -> ClassAnswer | None:
        """The answer to question ``name``, or None when the adapter left it out."""
        return dict(self.answers).get(name)


FastRequest = ExtractionRequest | ClassificationRequest
FastResult = ExtractionResult | ClassificationResult

# --- what code makes of a fast-class answer -------------------------------------------

Outcome = Literal["accept", "abstain"]


@dataclass(frozen=True)
class Judgement:
    """What the thresholds made of one answer: accept with a value, or abstain, and why.

    ``value`` is the accepted value (None for an accepted "absent" field) and the answer's
    own value on an abstention, kept for the record. ``step`` is the step that answered.
    """

    name: str
    outcome: Outcome
    value: str | bool | tuple[str, ...] | None
    reason: str
    spans: tuple[SpanRef, ...] = ()
    step: str | None = None

    @property
    def accepted(self) -> bool:
        """Whether the answer is used."""
        return self.outcome == "accept"
