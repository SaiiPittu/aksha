"""Recorded model steps of an earlier run, keyed by the run-independent step key (D7, MT-8).

A step ID is ``R-0002/compare[C1]/t03/c02/d01``; its **step key** is the same ID without
the run, ``compare[C1]/t03/c02/d01``, because a replayed run has a new run ID but walks
the same Task instances, turns and calls. :func:`recorded_model_steps` reads one run's
completed model steps from the log; the replay adapters (``adapters/models/replay.py``)
answer from them and refuse a request whose hash differs from the recorded one
(``replay_mismatch``) or a step the run never recorded (``replay_missing``), so a replay
never invents an answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from aksha.runtime.events.envelope import Event
from aksha.runtime.execution.types import StepId


def step_key(step_id: str | StepId) -> str:
    """The step ID without its run: ``compare[C1]/t03/c02/d01``; ``""`` for the run itself."""
    text = str(step_id)
    StepId.parse(text)
    _, _, rest = text.partition("/")
    return rest


@dataclass(frozen=True)
class RecordedStep:
    """One completed model step as an earlier run recorded it."""

    key: str
    model_class: str
    alias: str
    native: bool
    args_hash: str
    value: Mapping[str, Any]


def recorded_model_steps(events: Iterable[Event], run: str) -> Mapping[str, RecordedStep]:
    """Every model step of ``run`` that completed ``ok``, by step key."""
    started: dict[str, Mapping[str, Any]] = {}
    out: dict[str, RecordedStep] = {}
    for e in events:
        if e.run != run or e.step is None or e.payload.get("kind") != "model":
            continue
        if e.type == "step.started":
            started[e.step] = e.payload
        elif e.type == "step.completed" and e.payload.get("outcome") == "ok":
            intent = started.get(e.step)
            value = e.payload.get("value")
            if intent is None or not isinstance(value, Mapping):
                continue
            key = step_key(e.step)
            out[key] = RecordedStep(
                key=key,
                model_class=str(intent.get("model_class", "generation")),
                alias=str(intent.get("alias", intent.get("name", ""))),
                native=bool(intent.get("native", True)),
                args_hash=str(intent.get("args_hash", "")),
                value=value,
            )
    return MappingProxyType(out)
