"""Record every model call of a run, and answer the same calls again offline (ADR-P22 A1).

The replay of ADR-P22 D7 answers a run's top-level generation steps from its event log.
A tool body's fast-class calls (ADR-P07 G1) are summarised on the tool step, not stored
call by call, so a run with fast readers cannot be replayed from its log alone. A
recording keeps them all: :func:`recording_adapters` wraps each bound adapter so every
answer is appended to the recording's ``models`` stream, keyed by what was asked:

* a generation by its step key (the step ID without the run) and its context hash;
* an extraction or a classification by its alias and its request hash (a tool body's
  requests carry no step of their own), in call order when one request is asked more
  than once (a call that failed and its retry).

A call the adapter refused (a timeout, a rate limit, a context overflow) is appended too,
with its error, so a replay fails the same call the same way and the step retries as it
did. A tool step whose body timed out and then ran again asks only the request that
failed (ADR-P07 G8: earlier answers are remembered for the run); a recording made before
G8 asked them all again, and only the record of the failure says which answers belonged
to the failed attempt.

:func:`recorded_adapters` answers from those lines: a generation whose step key the run
never recorded is ``replay_missing``, one asked something else ``replay_mismatch``; a
fast-class request the run never asked is ``replay_missing``. A generation step replays
its recorded failures in order, then its last answer (a step asked again after a crash
used the later answer); a fast-class request replays every line in call order, failures
included, and the last one repeats. Each is a
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
from aksha.runtime.models.errors import (
    ContextOverflow,
    PermanentModelError,
    TransientModelError,
)
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


def _error(e: TransientModelError | PermanentModelError | ContextOverflow) -> dict[str, Any]:
    """A refused call as a recording line keeps it: which refusal, its code and message."""
    kind = ("transient" if isinstance(e, TransientModelError)
            else "overflow" if isinstance(e, ContextOverflow) else "permanent")  # fmt: skip
    out: dict[str, Any] = {"kind": kind, "code": e.code, "message": e.message}
    if e.detail:
        out["detail"] = dict(e.detail)
    if isinstance(e, TransientModelError) and e.retry_after_ms:
        out["retry_after_ms"] = e.retry_after_ms
    return out


def _raise(err: Mapping[str, Any]) -> None:
    """Raise the refusal ``err`` (from :func:`_error`) again."""
    code, message = str(err.get("code") or ""), str(err.get("message", ""))
    detail = err.get("detail") if isinstance(err.get("detail"), Mapping) else None
    kind = err.get("kind")
    if kind == "transient":
        after = err.get("retry_after_ms")
        raise TransientModelError(code or None, message, detail=detail,
                                  retry_after_ms=int(after) if after else None)  # fmt: skip
    if kind == "overflow":
        raise ContextOverflow(code or None, message, detail=detail)
    raise PermanentModelError(code or None, message, detail=detail)


_REFUSALS = (TransientModelError, PermanentModelError, ContextOverflow)


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
        """The inner answer, recorded under the step key and the context hash (or its error)."""
        line = {"class": "generation", "alias": self.alias, "key": step_key(request.step_id),
                "hash": request.context_hash}  # fmt: skip
        try:
            result: GenerationResult = self.inner.generate(request)
        except _REFUSALS as e:
            self.sink.append(STREAM, {**line, "error": _error(e)})
            raise
        self.sink.append(STREAM, {**line, "result": generation_result_json(result)})
        return result


class RecordingExtraction(_Recording):
    """An extraction adapter whose every answer is appended to the recording."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """The inner answer, recorded under the alias and the request hash (or its error)."""
        line = {"class": "extraction", "alias": self.alias, "hash": request.request_hash,
                "native": self.capabilities.native}  # fmt: skip
        try:
            result: ExtractionResult = self.inner.extract(request)
        except _REFUSALS as e:
            self.sink.append(STREAM, {**line, "error": _error(e)})
            raise
        self.sink.append(STREAM, {**line, "answers": extraction_result_json(result)})
        return result


class RecordingClassification(_Recording):
    """A classification adapter whose every answer is appended to the recording."""

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """The inner answer, recorded under the alias and the request hash (or its error)."""
        line = {"class": "classification", "alias": self.alias, "hash": request.request_hash,
                "native": self.capabilities.native}  # fmt: skip
        try:
            result: ClassificationResult = self.inner.classify(request)
        except _REFUSALS as e:
            self.sink.append(STREAM, {**line, "error": _error(e)})
            raise
        self.sink.append(STREAM, {**line, "answers": classification_result_json(result)})
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
        steps: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        self.fast: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
        self._next: dict[tuple[str, ...], int] = defaultdict(int)
        self.natives: dict[str, bool] = {}
        for line in lines:
            kind = str(line["class"])
            if kind == "generation":
                steps[str(line["key"])].append(line)
            else:
                alias = str(line["alias"])
                self.fast[(kind, alias, str(line["hash"]))].append(line)
                if "error" not in line:
                    native = bool(line.get("native", True))
                    self.natives[alias] = self.natives.get(alias, False) or native
        self.generations: dict[str, list[Mapping[str, Any]]] = {
            key: _attempts(found) for key, found in steps.items()
        }

    def _take(self, slot: tuple[str, ...], found: list[Mapping[str, Any]]) -> Mapping[str, Any]:
        n = self._next[slot]
        self._next[slot] = n + 1
        return found[min(n, len(found) - 1)]

    def generation(self, request: GenerationRequest) -> GenerationResult:
        """The recorded answer (or the recorded failure) of this attempt of a step."""
        key = step_key(request.step_id)
        found = self.generations.get(key)
        if found is None:
            raise PermanentModelError(REPLAY_MISSING, f"no recorded model step {key}")
        line = self._take(("generation", key), found)
        if line["hash"] != request.context_hash:
            raise PermanentModelError(REPLAY_MISMATCH, f"step {key} was asked something else")
        if "error" in line:
            _raise(line["error"])
        return replace(generation_result(line["result"]), usage=Usage())

    def answers(self, kind: str, alias: str, request_hash: str) -> Any:
        """The recorded answers of this fast-class request, in call order (the last repeats).

        A recorded failure is raised again in its place.
        """
        slot = (kind, alias, request_hash)
        found = self.fast.get(slot)
        if not found:
            raise PermanentModelError(REPLAY_MISSING, f"no recorded {kind} {alias} {request_hash}")
        line = self._take(slot, found)
        if "error" in line:
            _raise(line["error"])
        return line["answers"]


def _attempts(lines: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """A step's attempts as a replay makes them: its failures in order, then its last answer.

    An answer before the last one was asked again (the step crashed before it completed), so
    the run used the later one; failures after the last answer stay, in order.
    """
    answered = [i for i, line in enumerate(lines) if "error" not in line]
    if not answered:
        return lines
    last = answered[-1]
    return [*(line for line in lines[:last] if "error" in line), *lines[last:]]


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
