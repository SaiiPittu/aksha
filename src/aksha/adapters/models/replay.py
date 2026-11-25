"""Replay adapters: answer each model call from an earlier run's recorded step (``replay``).

Each adapter looks the request's step up by its run-independent step key
(:func:`aksha.runtime.models.replay.step_key`) and returns the recorded result:

1. ``replay_missing``: the earlier run recorded no completed model step under that key.
2. ``replay_mismatch``: the recorded step was asked something else (its ``args_hash``,
   the context or request hash, differs), so the recording does not describe this call.

Both are a ``PermanentModelError``; a replay never falls through to another adapter's
answer. A replay adapter is offline and keeps the recorded answer's ``native`` flag, so a
cascade replays the same chain. :func:`replay_adapters` builds one per alias.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import ClassVar, Literal

from aksha.runtime.models.codec import classification_result, extraction_result, generation_result
from aksha.runtime.models.errors import PermanentModelError
from aksha.runtime.models.replay import RecordedStep, step_key
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

REPLAY_MISSING = "replay_missing"
REPLAY_MISMATCH = "replay_mismatch"


class _Replay:
    def __init__(self, recorded: Mapping[str, RecordedStep], *, native: bool = True) -> None:
        self.recorded = recorded
        self.calls: list[str] = []
        self._native = native

    def _step(self, step_id: str, args_hash: str) -> RecordedStep:
        key = step_key(step_id)
        self.calls.append(key)
        found = self.recorded.get(key)
        if found is None:
            raise PermanentModelError(REPLAY_MISSING, f"no recorded model step {key}")
        if found.args_hash != args_hash:
            raise PermanentModelError(REPLAY_MISMATCH, f"step {key} was asked something else")
        return found


class ReplayGenerationModel(_Replay):
    """Generation answers from the recording."""

    model_class: ClassVar[Literal["generation"]] = "generation"
    capabilities: ClassVar[ModelCapabilities] = ModelCapabilities(native=True, offline=True)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """The recorded result of this step; a replay spends nothing, so its usage is zero."""
        step = self._step(request.step_id, request.context_hash)
        return replace(generation_result(step.value["result"]), usage=Usage())


class ReplayExtractionModel(_Replay):
    """Extraction answers from the recording."""

    model_class: ClassVar[Literal["extraction"]] = "extraction"

    @property
    def capabilities(self) -> ModelCapabilities:
        """Native and span-returning, as the recorded extractor was."""
        return ModelCapabilities(
            native=self._native, field_types=FIELD_TYPES, returns_spans=True, offline=True
        )

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """The recorded answers of this step."""
        return extraction_result(self._step(request.step_id, request.request_hash).value["answers"])


class ReplayClassificationModel(_Replay):
    """Classification answers from the recording, native or adapted as recorded."""

    model_class: ClassVar[Literal["classification"]] = "classification"

    @property
    def capabilities(self) -> ModelCapabilities:
        """A classifier with a distribution, native or adapted as the recording says."""
        return ModelCapabilities(
            native=self._native,
            question_types=QUESTION_TYPES,
            returns_distribution=True,
            offline=True,
        )

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """The recorded answers of this step."""
        step = self._step(request.step_id, request.request_hash)
        return classification_result(step.value["answers"])


def replay_adapters(
    aliases: Iterable[ModelAlias], recorded: Mapping[str, RecordedStep]
) -> dict[str, object]:
    """One replay adapter per alias (and ``generate``), each over the whole recording.

    An alias whose recorded steps all answered adapted (``native=False``) replays as
    adapted, so the cascade's order and its adapted thresholds are the recorded ones.
    """
    out: dict[str, object] = {}
    for name, alias in declared_aliases(aliases).items():
        mine = [r for r in recorded.values() if r.alias == name]
        native = not mine or any(r.native for r in mine)
        if alias.model_class == "generation":
            out[name] = ReplayGenerationModel(recorded)
        elif alias.model_class == "extraction":
            out[name] = ReplayExtractionModel(recorded, native=native)
        else:
            out[name] = ReplayClassificationModel(recorded, native=native)
    return out
