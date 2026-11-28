"""Model calls as durable steps, and the cascade of a fast-class alias (ADR-P07 D6 to D8).

Every model call, of every class, is a step of the durable step executor under a
deterministic step ID, so a resume returns the recorded answer and a second fake-mode run
writes the same bytes:

1. :func:`generation_step` runs one generation call. ``step.started`` carries
   ``model_class``, ``alias``, ``adapter`` and ``native`` with the context hash as its
   ``args_hash``; ``step.completed`` carries the result and the call's ``usage``, which
   the runtime records here and only here.
2. :func:`run_cascade` runs a fast-class alias's chain (``routing.cascade_for``), one
   step per candidate, ``<call>/d01``, ``<call>/d02``, each naming the one it
   ``replaces``. Code judges every answer in the step and records the judgement beside
   the answer; the next candidate is asked only what is still unaccepted. When the chain
   is used up (or empty: a question no candidate was asked is ``not_asked``), the
   alias's ``on_exhausted`` says what follows (``unknown`` with a
   question for a machine-only artifact, ``review`` for a reviewed one, or ``fail``).
3. A ``TransientModelError`` is retried with backoff on the injected clock (twice from
   500 ms by default; the agent loop passes ``Limits.max_retries`` and ``Limits.backoff``,
   which live mode raises, ADR-P14 B2), waiting at least the provider's ``Retry-After``;
   then the step ends ``model_transient`` and the call is ``model_unavailable``. A permanent
   error ends the step with its own code; in a cascade the next candidate is tried. A
   provider account with no credits left (``ModelQuotaExhausted``) is never retried and
   never ends the step: the attempt is recorded and the step is suspended
   (``StepSuspended``), so the run stays open at this step and ``aksha resume`` asks again
   once the account is topped up. What
   the provider said (an adapter's ``detail``: error type, status, short message) is
   recorded on each ``step.failed{error}`` and on an error ``step.completed{error}``.

A fast-class call is never made inside a completion check (P-3), and its answer can only
refuse what a code rule allowed: nothing here authorises an effect.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, cast

from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.errors import StepSuspended, TransientStepError
from aksha.runtime.execution.ports import DurableStepExecutor
from aksha.runtime.execution.types import Deadline, StepId, StepOutput, StepPolicy, StepResult
from aksha.runtime.models.codec import (
    classification_result,
    classification_result_json,
    extraction_result,
    extraction_result_json,
    generation_result,
    generation_result_json,
    judgement,
    judgement_json,
)
from aksha.runtime.models.errors import (
    ContextOverflow,
    ModelQuotaExhausted,
    PermanentModelError,
    TransientModelError,
)
from aksha.runtime.models.ports import ClassificationModel, ExtractionModel
from aksha.runtime.models.routing import AliasTable, Candidate, adapter_name, cascade_for
from aksha.runtime.models.thresholds import threshold_classification, threshold_extraction
from aksha.runtime.models.types import (
    FINISH_REASONS,
    ClassificationRequest,
    ExtractionRequest,
    FastRequest,
    GenerationRequest,
    GenerationResult,
    Judgement,
    OnExhausted,
    Usage,
)

MODEL_TRANSIENT = "model_transient"
MODEL_UNAVAILABLE = "model_unavailable"
RESULT_INVALID = "result_invalid"

MODEL_STEP_POLICY = StepPolicy(
    timeout_ms=60_000, max_retries=2, retry_on=frozenset({MODEL_TRANSIENT, "timeout"})
)
"""Two retries with backoff on a transient error or a timeout (D8)."""


def _call(work: Callable[[], StepOutput]) -> StepOutput:
    """Run an adapter call, mapping its errors onto the step executor's outcomes."""
    try:
        return work()
    except TransientModelError as e:
        raise TransientStepError(
            MODEL_TRANSIENT, e.message, detail=e.detail, retry_after_ms=e.retry_after_ms
        ) from e
    except ModelQuotaExhausted as e:
        raise StepSuspended(e.code, e.message, detail=e.detail) from e
    except (ContextOverflow, PermanentModelError) as e:
        return StepOutput(outcome="error", code=e.code, error=e.detail)


def _failure_code(step: StepResult) -> str:
    if step.code in (MODEL_TRANSIENT, "timeout"):
        return MODEL_UNAVAILABLE
    return step.code or step.outcome


def _extra(table: AliasTable, alias: str, adapter: object, native: bool) -> dict[str, Any]:
    return {
        "model_class": table.aliases[alias].model_class,
        "alias": alias,
        "adapter": adapter_name(adapter),
        "native": native,
    }


@dataclass(frozen=True)
class GenerationOutcome:
    """A generation step's end: the result, or the code it failed with."""

    step: StepResult
    result: GenerationResult | None
    code: str | None = None


def generation_step(
    executor: DurableStepExecutor,
    step_id: StepId,
    table: AliasTable,
    request: GenerationRequest,
    *,
    policy: StepPolicy = MODEL_STEP_POLICY,
    started: Mapping[str, Any] | None = None,
    facts: Callable[[GenerationResult], Sequence[NewEvent]] | None = None,
) -> GenerationOutcome:
    """Run (or replay) one generation call of ``request.alias`` as step ``step_id``.

    ``started`` adds fields to ``step.started`` (the context's size, renderings and masks,
    ADR-P08). ``facts`` builds the events that commit with the result (U2): the reads and
    spans the context showed, or a summary step's ``context.compacted``.
    """
    if request.step_id != str(step_id):
        raise ValueError(f"the request names step {request.step_id}, not {step_id}")
    adapter = table.generation(request.alias)

    def body() -> StepOutput:
        result = adapter.generate(request)
        if result.finish not in FINISH_REASONS:
            return StepOutput(outcome="error", code=RESULT_INVALID)
        value = {"result": generation_result_json(result)}
        events = tuple(facts(result)) if facts is not None else ()
        return StepOutput(value, usage=result.usage.as_json(), events=events)

    def work(deadline: Deadline) -> StepOutput:
        return _call(body)

    extra = {
        **(started or {}),
        **_extra(table, request.alias, adapter, table.native(request.alias)),
    }
    step = executor.step(
        step_id, "model", request.alias, request.context_hash, work, policy, extra=extra
    )
    if step.outcome != "ok" or step.value is None:
        return GenerationOutcome(step, None, _failure_code(step))
    return GenerationOutcome(step, generation_result(step.value["result"]))


@dataclass(frozen=True)
class CascadeResult:
    """What a fast-class alias's cascade made of one request.

    ``judgements`` holds one per field or question, in request order: the accepting
    candidate's, or the last abstention. ``exhausted`` names what no candidate accepted;
    ``action`` is what follows for them (None when nothing is exhausted).
    """

    alias: str
    judgements: tuple[Judgement, ...]
    exhausted: tuple[str, ...]
    action: OnExhausted | None
    steps: tuple[StepResult, ...]
    failures: tuple[tuple[str, str], ...] = ()

    def accepted(self) -> Mapping[str, object]:
        """The accepted value of each accepted field or question, by name."""
        return {j.name: j.value for j in self.judgements if j.accepted}


def _names(request: FastRequest) -> tuple[str, ...]:
    if isinstance(request, ExtractionRequest):
        return tuple(f.name for f in request.fields)
    return tuple(q.name for q in request.questions)


def _narrow(request: FastRequest, pending: set[str], step: StepId) -> FastRequest:
    if isinstance(request, ExtractionRequest):
        fields = tuple(f for f in request.fields if f.name in pending)
        return replace(request, step_id=str(step), fields=fields)
    questions = tuple(q for q in request.questions if q.name in pending)
    return replace(request, step_id=str(step), questions=questions)


def _judge(request: FastRequest, candidate: Candidate) -> StepOutput:
    """Ask ``candidate`` and judge every answer, in code, inside the step."""
    if isinstance(request, ExtractionRequest):
        extracted = cast(ExtractionModel, candidate.adapter).extract(request)
        state = {s.span_id: s for s in request.state}
        found = [
            threshold_extraction(extracted.answer(f.name), f, state, candidate.native)
            for f in request.fields
        ]
        answers: object = extraction_result_json(extracted)
        usage = extracted.usage
    else:
        classified = cast(ClassificationModel, candidate.adapter).classify(request)
        found = [
            threshold_classification(classified.answer(q.name), q, candidate.native)
            for q in request.questions
        ]
        answers = classification_result_json(classified)
        usage = classified.usage
    value = {
        "native": candidate.native,
        "answers": answers,
        "judgements": [judgement_json(j) for j in found],
    }
    return StepOutput(value, usage=None if usage == Usage() else usage.as_json())


def recorded_answers(value: Mapping[str, Any], kind: str) -> object:
    """A fast-class step's recorded adapter result, as the result type of ``kind``."""
    if kind == "extraction":
        return extraction_result(value["answers"])
    return classification_result(value["answers"])


def run_cascade(
    executor: DurableStepExecutor,
    base: StepId,
    table: AliasTable,
    request: ExtractionRequest | ClassificationRequest,
    *,
    reviewable: bool = False,
    policy: StepPolicy = MODEL_STEP_POLICY,
) -> CascadeResult:
    """Run (or replay) ``request.alias``'s cascade under ``base``, a call step (module doc).

    ``reviewable`` says the answer feeds a human-reviewed artifact, so an alias with no
    ``on_exhausted`` asks for a review instead of leaving an explicit unknown.
    """
    if base.call is None or base.sub is not None:
        raise ValueError(f"a cascade runs under a call step, not {base}")
    alias = table.aliases[request.alias]
    names = _names(request)
    pending = set(names)
    last: dict[str, Judgement] = {}
    steps: list[StepResult] = []
    failures: list[tuple[str, str]] = []
    previous: str | None = None
    for n, candidate in enumerate(cascade_for(request.alias, table), start=1):
        if not pending:
            break
        sid = StepId(base.run, base.instance, turn=base.turn, call=base.call, sub=f"d{n:02d}")
        asked = _narrow(request, pending, sid)

        def work(
            deadline: Deadline, asked: FastRequest = asked, c: Candidate = candidate
        ) -> StepOutput:
            return _call(lambda: _judge(asked, c))

        extra = _extra(table, candidate.alias, candidate.adapter, candidate.native)
        if previous is not None:
            extra["replaces"] = previous
        step = executor.step(
            sid, "model", candidate.alias, asked.request_hash, work, policy, extra=extra
        )
        steps.append(step)
        previous = str(sid)
        if step.outcome != "ok" or step.value is None:
            failures.append((str(sid), _failure_code(step)))
            continue
        for item in step.value["judgements"]:
            j = judgement(item, str(sid))
            if j.name in pending:
                last[j.name] = j
                if j.accepted:
                    pending.discard(j.name)
    judged = tuple(
        last.get(name) or Judgement(name, "abstain", None, "not_asked") for name in names
    )
    exhausted = tuple(name for name in names if name in pending)
    action: OnExhausted | None = None
    if exhausted:
        action = alias.on_exhausted or ("review" if reviewable else "unknown")
    return CascadeResult(request.alias, judged, exhausted, action, tuple(steps), tuple(failures))
