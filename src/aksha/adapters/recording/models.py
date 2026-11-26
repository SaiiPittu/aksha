"""Record every model call of a run, and answer the same calls again offline (ADR-P22 A1).

The replay of ADR-P22 D7 answers a run's top-level generation steps from its event log.
A tool body's fast-class calls (ADR-P07 G1) are summarised on the tool step, not stored
call by call, so a run with fast readers cannot be replayed from its log alone. A
recording keeps them all: :func:`recording_adapters` wraps each bound adapter so every
answer is appended to the recording's ``models`` stream, keyed by what was asked:

* a generation by its step key (the step ID without the run) and its context hash;
* an extraction or a classification by its alias and its request hash (a tool body's
  requests carry no step of their own), in call order when one request is asked twice.

:func:`recorded_adapters` answers from those lines: a generation whose step key the run
never recorded is ``replay_missing``, one asked something else ``replay_mismatch``; a
fast-class request the run never asked is ``replay_missing``. Each is a
``PermanentModelError``, so a replay never falls through to another adapter's answer.
Usage replays as zero: a replay spends nothing.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from aksha.runtime.models.codec import (
    classification_result,
    classification_result_json,
    extraction_result,
    extraction_result_json,
    generation_result,
    generation_result_json,
)
from aksha.runtime.models.errors import PermanentModelError
from aksha.runtime.models.replay import step_key
from aksha.runtime.models.routing import declared_aliases
from aksha.runtime.models.types import (
    FIELD_TYPES,
    QUESTION_TYPES,
    ClassificationRequest,
    ClassificationResult,
    ExtractionRequest,
    ExtractionResult,
    GenerationRequest,
    GenerationResult,
    ModelAlias,
    ModelCapabilities,
    Usage,
)

from .sink import Sink

STREAM = "models"
REPLAY_MISSING = "replay_missing"
REPLAY_MISMATCH = "replay_mismatch"
"""The refusal codes of the event-log replay (``adapters.models.replay``), the same here."""


class _Recording:
    def __init__(self, alias: str, inner: Any, sink: Sink) -> None:
        self.alias = alias
        self.inner = inner
        self.sink = sink

    @property
    def model_class(self) -> str:
        return str(self.inner.model_class)

    @property
    def capabilities(self) -> ModelCapabilities:
        caps: ModelCapabilities = self.inner.capabilities
        return caps

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class RecordingGeneration(_Recording):
    """A generation adapter whose every answer is appended to the recording."""

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """The inner answer, recorded under the step key and the context hash."""
        result: GenerationResult = self.inner.generate(request)
        self.sink.append(STREAM, {"class": "generation", "alias": self.alias,
                                  "key": step_key(request.step_id), "hash": request.context_hash,
                                  "result": generation_result_json(result)})  # fmt: skip
        return result


class RecordingExtraction(_Recording):
    """An extraction adapter whose every answer is appended to the recording."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """The inner answer, recorded under the alias and the request hash."""
        result: ExtractionResult = self.inner.extract(request)
        self.sink.append(STREAM, {"class": "extraction", "alias": self.alias,
                                  "hash": request.request_hash, "native": self.capabilities.native,
                                  "answers": extraction_result_json(result)})  # fmt: skip
        return result


class RecordingClassification(_Recording):
    """A classification adapter whose every answer is appended to the recording."""

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """The inner answer, recorded under the alias and the request hash."""
        result: ClassificationResult = self.inner.classify(request)
        self.sink.append(STREAM, {"class": "classification", "alias": self.alias,
                                  "hash": request.request_hash, "native": self.capabilities.native,
                                  "answers": classification_result_json(result)})  # fmt: skip
        return result


_WRAPPERS: Mapping[str, type[_Recording]] = {
    "generation": RecordingGeneration,
    "extraction": RecordingExtraction,
    "classification": RecordingClassification,
}


def recording_adapters(adapters: Mapping[str, object], sink: Sink) -> dict[str, object]:
    """Each adapter of ``adapters`` (by alias) wrapped to record into ``sink``."""
    out: dict[str, object] = {}
    for alias, adapter in adapters.items():
        wrapper = _WRAPPERS.get(str(getattr(adapter, "model_class", "")))
        out[alias] = adapter if wrapper is None else wrapper(alias, adapter, sink)
    return out


class Recorded:
    """A recording's ``models`` lines, indexed for answering (module doc)."""

    def __init__(self, lines: Iterable[Mapping[str, Any]]) -> None:
        """Index ``lines`` in recorded order."""
        self.generations: dict[str, Mapping[str, Any]] = {}
        self.fast: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
        self._next: dict[tuple[str, str, str], int] = defaultdict(int)
        self.natives: dict[str, bool] = {}
        for line in lines:
            kind = str(line["class"])
            if kind == "generation":
                self.generations[str(line["key"])] = line
            else:
                alias = str(line["alias"])
                self.fast[(kind, alias, str(line["hash"]))].append(line)
                native = bool(line.get("native", True))
                self.natives[alias] = self.natives.get(alias, False) or native

    def generation(self, request: GenerationRequest) -> GenerationResult:
        """The recorded answer of this generation step."""
        key = step_key(request.step_id)
        found = self.generations.get(key)
        if found is None:
            raise PermanentModelError(REPLAY_MISSING, f"no recorded model step {key}")
        if found["hash"] != request.context_hash:
            raise PermanentModelError(REPLAY_MISMATCH, f"step {key} was asked something else")
        return replace(generation_result(found["result"]), usage=Usage())

    def answers(self, kind: str, alias: str, request_hash: str) -> Any:
        """The recorded answers of this fast-class request, in call order (the last repeats)."""
        slot = (kind, alias, request_hash)
        found = self.fast.get(slot)
        if not found:
            raise PermanentModelError(REPLAY_MISSING, f"no recorded {kind} {alias} {request_hash}")
        n = self._next[slot]
        self._next[slot] = n + 1
        return found[min(n, len(found) - 1)]["answers"]


class _Answering:
    offline_caps: ModelCapabilities

    def __init__(self, alias: str, recorded: Recorded) -> None:
        self.alias = alias
        self.recorded = recorded

    @property
    def capabilities(self) -> ModelCapabilities:
        """Offline, and native or adapted as the recorded adapter answered."""
        return replace(self.offline_caps, native=self.recorded.natives.get(self.alias, True))


class RecordedGeneration(_Answering):
    """Generation answered from a recording."""

    model_class = "generation"
    offline_caps = ModelCapabilities(native=True, offline=True)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """The recorded answer of this step."""
        return self.recorded.generation(request)


class RecordedExtraction(_Answering):
    """Extraction answered from a recording."""

    model_class = "extraction"
    offline_caps = ModelCapabilities(native=True, field_types=FIELD_TYPES, returns_spans=True,
                                     offline=True)  # fmt: skip

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """The recorded answers of this request."""
        return extraction_result(self.recorded.answers("extraction", self.alias,
                                                       request.request_hash))  # fmt: skip


class RecordedClassification(_Answering):
    """Classification answered from a recording."""

    model_class = "classification"
    offline_caps = ModelCapabilities(native=True, question_types=QUESTION_TYPES,
                                     returns_distribution=True, offline=True)  # fmt: skip

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """The recorded answers of this request."""
        return classification_result(self.recorded.answers("classification", self.alias,
                                                           request.request_hash))  # fmt: skip


def recorded_adapters(aliases: Iterable[ModelAlias], recorded: Recorded) -> dict[str, object]:
    """One recorded-answer adapter per declared alias (and ``generate``)."""
    out: dict[str, object] = {}
    for name, alias in declared_aliases(aliases).items():
        if alias.model_class == "generation":
            out[name] = RecordedGeneration(name, recorded)
        elif alias.model_class == "extraction":
            out[name] = RecordedExtraction(name, recorded)
        else:
            out[name] = RecordedClassification(name, recorded)
    return out
