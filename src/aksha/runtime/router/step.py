"""The route classification as a durable step in the session's namespace (ADR-P23 D2).

The step executor (ADR-P02) runs a run's steps; a route comes before any run, so its one
classification is recorded here under the same facts and keys, in the session's envelope
(``session`` set, no run) at :func:`~aksha.runtime.router.decisions.route_step_id`:

1. A recorded ``step.completed`` is returned as it is: a restart never re-routes.
2. Otherwise ``step.started`` commits once (U2a, ``at_least_once``), naming the alias,
   the adapter and whether it is native, with the request hash as its ``args_hash``; a
   recorded start under another hash is ``StepMismatch`` (the loaded agents changed).
3. The classifier is asked and code judges the answer (ADR-P07 D4). ``step.completed``
   carries the answer and the judgement, and commits with the facts ``facts`` builds from
   them (U2): the route question when the answer abstains, so the two are one fact. A
   model error ends the step ``error`` with its code (``model_unavailable`` for a
   transient one); it is not retried here, because the router asks instead (D3).

A started step with no completion is classified again on the next call, which is what
``at_least_once`` allows: the call has no effect outside the store.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution.errors import StepMismatch
from aksha.runtime.models.codec import classification_result_json, judgement, judgement_json
from aksha.runtime.models.errors import ContextOverflow, PermanentModelError, TransientModelError
from aksha.runtime.models.ports import ClassificationModel
from aksha.runtime.models.routing import adapter_name
from aksha.runtime.models.thresholds import threshold_classification
from aksha.runtime.models.types import ClassificationRequest, Judgement
from aksha.runtime.storage.errors import AlreadyRecorded
from aksha.runtime.storage.ports import EventStore

STEP_INTENT = "U2a"
STEP_RESULT = "U2"
MODEL_UNAVAILABLE = "model_unavailable"


@dataclass(frozen=True)
class RouteStep:
    """A recorded route classification: the judgement, or the code the call failed with."""

    step_id: str
    judgement: Judgement | None
    code: str | None
    probabilities_ppm: tuple[tuple[str, int], ...] = ()

    @classmethod
    def from_event(cls, e: Event) -> RouteStep:
        """Read a recorded ``step.completed`` back."""
        step = str(e.step)
        if e.payload.get("outcome") != "ok":
            return cls(step, None, str(e.payload.get("code", "error")))
        value = e.payload["value"]
        answers = value.get("answers") or [{}]
        probs = answers[0].get("probabilities_ppm") or []
        ranked = tuple((str(k), int(p)) for k, p in probs)
        return cls(step, judgement(value["judgement"], step), None, ranked)


def recorded(store: EventStore, owner: str, step_id: str, type_: str) -> Event | None:
    """The recorded ``type_`` event of ``step_id``, or None."""
    key = f"{'sdone' if type_ == 'step.completed' else 'sstart'}:{step_id}"
    return store.by_dedup_key(owner, key)


def _started(request: ClassificationRequest, model: ClassificationModel) -> dict[str, Any]:
    return {
        "kind": "model",
        "name": request.alias,
        "args_hash": request.request_hash,
        "semantics": "at_least_once",
        "counted": False,
        "model_class": "classification",
        "alias": request.alias,
        "adapter": adapter_name(model),
        "native": model.capabilities.native,
    }


def _ask(request: ClassificationRequest, model: ClassificationModel) -> dict[str, Any]:
    """The ``step.completed`` payload of one classification, judged in code."""
    question = request.questions[0]
    try:
        result = model.classify(request)
    except TransientModelError:
        return {"outcome": "error", "kind": "model", "name": request.alias,
                "code": MODEL_UNAVAILABLE}  # fmt: skip
    except (ContextOverflow, PermanentModelError) as e:
        return {"outcome": "error", "kind": "model", "name": request.alias, "code": e.code}
    judged = threshold_classification(
        result.answer(question.name), question, model.capabilities.native
    )
    value = {
        "native": model.capabilities.native,
        "answers": classification_result_json(result),
        "judgement": judgement_json(judged),
    }
    return {"outcome": "ok", "kind": "model", "name": request.alias, "value": value}


def _complete(store: EventStore, owner: str, events: Sequence[NewEvent]) -> None:
    """Commit ``step.completed`` and its facts in one transaction (U2)."""
    store.append(owner, STEP_RESULT, events)


def route_step(
    store: EventStore,
    owner: str,
    session: str,
    request: ClassificationRequest,
    model: ClassificationModel,
    facts: Callable[[RouteStep], Sequence[NewEvent]],
) -> RouteStep:
    """Run (or return the record of) the classification ``request`` (module doc)."""
    step_id = request.step_id
    done = recorded(store, owner, step_id, "step.completed")
    if done is not None:
        return RouteStep.from_event(done)
    start = recorded(store, owner, step_id, "step.started")
    if start is None:
        started = NewEvent("step.started", _started(request, model), step=step_id, session=session)
        try:
            store.append(owner, STEP_INTENT, (started,))
        except AlreadyRecorded as e:
            start = e.existing
    if start is not None and start.payload.get("args_hash") != request.request_hash:
        raise StepMismatch(message=f"{step_id} was started for another route request")
    payload = _ask(request, model)
    completed = NewEvent("step.completed", payload, step=step_id, session=session)
    provisional = RouteStep.from_event(_as_event(completed))
    with contextlib.suppress(AlreadyRecorded):
        _complete(store, owner, (completed, *facts(provisional)))
    done = recorded(store, owner, step_id, "step.completed")
    assert done is not None
    return RouteStep.from_event(done)


def _as_event(e: NewEvent) -> Event:
    """A not-yet-stored completion read as an event, so one reader serves both."""
    return Event(e.type, e.payload, step=e.step, session=e.session, seq=0, owner="", at="",
                 boot=0, schema_version=1)  # fmt: skip
