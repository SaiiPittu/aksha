"""The step facts the durable step executor and recovery append, built in one place. Pure.

Every function returns the ``NewEvent`` for one fact and its boundary is fixed by type:

=====================  ========  =================================================
Event                  Boundary  When
=====================  ========  =================================================
``step.started``       U2a       before any attempt; carries ``args_hash``, ``semantics``
``step.dispatched``    U2b       ``at_most_once`` only, committed alone before the body
``step.failed``        U2c       a failed attempt, with whether it is retried
``attempt.discarded``  U2c       the late value of an abandoned attempt; never saved
``step.completed``     U2        the live attempt's value, with the facts it carries
``stop.decided``       U3        a resume record, which recovery writes (P02 D7)
=====================  ========  =================================================

Payloads hold no wall-clock reading and no monotonic reading. ``duration_ms`` (ADR-P14
active time) is read on the injected clock, and a timed-out attempt records its timeout,
so two fake runs write the same bytes. The dedup keys (``sstart:``, ``sdisp:``,
``sfail:``, ``sdisc:``, ``sdone:``) make each fact once per step (and per attempt).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.canonical import sha256_hex
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.types import (
    OUTCOME_UNKNOWN,
    DanglingStep,
    OnUnknown,
    StepId,
    StepKind,
    StepPolicy,
)

STEP_INTENT = "U2a"
DISPATCH = "U2b"
ATTEMPT_FAILED = "U2c"
STEP_RESULT = "U2"


def started_key(step: str) -> str:
    """The dedup key of a step's ``step.started``."""
    return f"sstart:{step}"


def completed_key(step: str) -> str:
    """The dedup key of a step's ``step.completed``."""
    return f"sdone:{step}"


def dispatched_key(step: str) -> str:
    """The dedup key of a step's ``step.dispatched``."""
    return f"sdisp:{step}"


def value_hash(value: Any) -> str:
    """``sha256:<hex>`` of the canonical JSON of ``value``: what a discarded value was."""
    return "sha256:" + sha256_hex(value)


def _envelope(step: StepId) -> dict[str, str | None]:
    return {"run": step.run, "instance": step.instance, "step": str(step)}


def step_started(
    step: StepId,
    kind: StepKind,
    name: str,
    args_hash: str,
    policy: StepPolicy,
    extra: Mapping[str, Any] | None = None,
) -> NewEvent:
    """The step intent. ``extra`` adds the gate's fields (route, position) to a tool step."""
    payload: dict[str, Any] = {
        "kind": kind,
        "name": name,
        "args_hash": args_hash,
        "semantics": policy.semantics,
        "counted": policy.counted,
    }
    if policy.semantics == "at_most_once":
        payload["on_unknown"] = policy.on_unknown
    if kind == "tool":
        payload["tool"] = name
    payload.update(extra or {})
    return NewEvent("step.started", payload, **_envelope(step))


def step_dispatched(step: StepId, attempt: int) -> NewEvent:
    """The dispatch marker of an ``at_most_once`` step: past it the body may have run."""
    return NewEvent("step.dispatched", {"attempt": attempt}, **_envelope(step))


def step_failed(
    step: StepId,
    attempt: int,
    code: str,
    *,
    retried: bool,
    backoff_ms: int,
    duration_ms: int | None = None,
    error: Mapping[str, str | int] | None = None,
) -> NewEvent:
    """A failed attempt; ``retried`` says another attempt follows after ``backoff_ms``.

    ``error`` is what the failure was when the work said (a provider's error type, status
    and short message, ADR-P07 G6); never a key or a request body.
    """
    payload: dict[str, Any] = {
        "attempt": attempt,
        "code": code,
        "retried": retried,
        "backoff_ms": backoff_ms,
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if error:
        payload["error"] = dict(error)
    return NewEvent("step.failed", payload, **_envelope(step))


def attempt_discarded(step: StepId, attempt: int, result_hash: str) -> NewEvent:
    """The late value of abandoned ``attempt``, recorded by its hash and dropped."""
    payload = {"attempt": attempt, "result_hash": result_hash}
    return NewEvent("attempt.discarded", payload, **_envelope(step))


def step_completed(
    step: StepId,
    kind: str,
    name: str | None,
    outcome: str,
    *,
    attempts: int,
    value: Mapping[str, Any] | None = None,
    code: str | None = None,
    on_unknown: OnUnknown | None = None,
    reconciled: bool = False,
    duration_ms: int | None = None,
    usage: Mapping[str, int] | None = None,
    kill: str | None = None,
    error: Mapping[str, str | int] | None = None,
) -> NewEvent:
    """The step's result: its outcome, code, recorded value and how many attempts it took.

    ``duration_ms`` is the last attempt's active time; ``usage`` a model step's spend;
    ``kill`` how the kill boundary ended the step's unit (ADR-P18 D6); ``error`` what an
    error outcome's provider said (as on ``step.failed``).
    """
    payload: dict[str, Any] = {"outcome": outcome, "kind": kind, "attempts": attempts}
    if kind == "tool" and name is not None:
        payload["tool"] = name
    elif name is not None:
        payload["name"] = name
    if code is not None:
        payload["code"] = code
    if value is not None:
        payload["value"] = dict(value)
    if on_unknown is not None:
        payload["on_unknown"] = on_unknown
    if reconciled:
        payload["reconciled"] = True
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if usage is not None:
        payload["usage"] = dict(usage)
    if kill is not None:
        payload["kill"] = kill
    if error:
        payload["error"] = dict(error)
    return NewEvent("step.completed", payload, **_envelope(step))


def unknown_completion(step: DanglingStep) -> NewEvent:
    """The close of a dispatched ``at_most_once`` step: ``interrupted``, ``outcome_unknown``.

    The step is never run again; the model or a person is told (``on_unknown``).
    """
    return step_completed(
        StepId.parse(step.step_id),
        step.kind,
        step.name,
        "interrupted",
        attempts=step.attempts,
        code=OUTCOME_UNKNOWN,
        on_unknown=step.on_unknown,
    )


def resume_record(
    run: str,
    walk: int,
    *,
    verdict: str,
    reason: str | None = None,
    limit: str | None = None,
    counts: Mapping[str, int] | None = None,
) -> NewEvent:
    """The stop decision recovery writes when it resumes ``run`` (ADR-P02 D7, D8).

    ``continue`` re-authorises the incomplete turn (``resumes_turn``, no turn charged);
    ``stop`` ends the run, with its reason, and for a limit the limit that bit and the
    count behind it.
    """
    payload: dict[str, Any] = {"walk": walk, "verdict": verdict, "cause": "resume"}
    if verdict == "continue":
        payload["resumes_turn"] = True
    if reason is not None:
        payload["reason"] = reason
    if limit is not None:
        payload["limit"] = limit
    if counts is not None:
        payload["counts"] = dict(counts)
    return NewEvent("stop.decided", payload, run=run)
