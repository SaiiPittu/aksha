"""An instance's turns as the event store recorded them, and the view a context is built from.

1. :func:`as_of` narrows a view to the moment before a step started. A context for a turn
   whose model step is already recorded is rebuilt from the log as it stood then, so a
   restart sends the same bytes (the context hash on ``step.started`` matches) however
   much was written since. A step not yet started sees the whole view.
2. :func:`instance_turns` reads one instance's recorded model turns: each model step
   ``<run>/<instance>/tNN`` that ended ``ok`` with its reply, and for each call in the reply
   the tool step ``.../tNN/cMM`` at its position, with the view it recorded under
   ``value["view"]``, the artifacts it saved, the inputs it read, the spans it minted and
   the span ID of each of its drafts (``value["spans"]``).
   A call whose step has no ``step.completed`` is shown as ``no_result``, so the call and
   its result stay paired. A step's payload is inflated from ``blobs`` first, so a reply
   or a view moved to the blob store (decision 25) reads as it was recorded.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.artifacts.blobs import inflate
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.context.types import ToolPair, Turn
from aksha.runtime.events.envelope import Event
from aksha.runtime.execution.errors import StepIdInvalid
from aksha.runtime.execution.types import StepId
from aksha.runtime.models.codec import generation_result
from aksha.runtime.projections.view import LogView
from aksha.runtime.tools.results import VIEW_FIELD, view_from_json

SPANS_FIELD = "spans"
"""The field of a tool step's recorded value that lists each span draft's ID, in order."""

NO_RESULT = "no_result"
"""The outcome shown for a call whose tool step has no recorded end."""


def as_of(view: LogView, step: StepId | str) -> LogView:
    """``view`` as it stood just before ``step`` started, or whole if it never started."""
    sid = str(step)
    for e in view.events:
        if e.type == "step.started" and e.step == sid:
            return view.upto(e.seq - 1)
    return view


def _parse(step: str | None) -> StepId | None:
    if step is None:
        return None
    try:
        return StepId.parse(step)
    except StepIdInvalid:
        return None


def _mine(sid: StepId | None, run: str, instance: str) -> bool:
    return sid is not None and sid.run == run and sid.instance == instance


def instance_turns(
    view: LogView, run: str, instance: str, *, blobs: BlobStore | None = None
) -> tuple[Turn, ...]:
    """The instance's recorded turns, oldest first (module doc, point 2)."""
    models: dict[int, Event] = {}
    tools: dict[tuple[int, int], Event] = {}
    saved: dict[str, list[str]] = {}
    reads: dict[str, list[InputRef]] = {}
    spans: dict[str, list[str]] = {}
    for e in view.events:
        if e.step is None:
            continue
        if e.type == "step.completed":
            sid = _parse(e.step)
            if not _mine(sid, run, instance) or sid is None or sid.turn is None:
                continue
            if sid.call is None and sid.sub is None and e.payload.get("outcome") == "ok":
                models[sid.turn] = e
            elif sid.call is not None and sid.sub is None:
                tools[(sid.turn, sid.call)] = e
        elif e.type == "artifact.saved":
            saved.setdefault(e.step, []).append(str(e.payload["ref"]))
        elif e.type == "read.recorded" and e.payload.get("via") == "tool":
            reads.setdefault(e.step, []).append(InputRef.from_json(e.payload["input"]))
        elif e.type == "excerpts.minted":
            spans.setdefault(e.step, []).extend(str(s["span_id"]) for s in e.payload["spans"])
    out: list[Turn] = []
    for number in sorted(models):
        event = models[number]
        value = inflate(event.payload, blobs).get("value") or {}
        result = generation_result(value.get("result", {}))
        pairs: list[ToolPair] = []
        for position, call in enumerate(result.tool_calls, start=1):
            step = str(StepId(run, instance, turn=number, call=position))
            pairs.append(
                _pair(
                    step,
                    number,
                    position,
                    call.call_id,
                    call.name,
                    call.arguments,
                    tools.get((number, position)),
                    blobs=blobs,
                    saved=tuple(saved.get(step, ())),
                    reads=tuple(reads.get(step, ())),
                    spans=tuple(spans.get(step, ())),
                )
            )
        out.append(Turn(number, str(event.step), result.text, result.tool_calls, tuple(pairs)))
    return tuple(out)


def _pair(
    step: str,
    turn: int,
    position: int,
    call_id: str,
    tool: str,
    arguments: Mapping[str, Any],
    done: Event | None,
    *,
    blobs: BlobStore | None = None,
    saved: tuple[str, ...] = (),
    reads: tuple[InputRef, ...] = (),
    spans: tuple[str, ...] = (),
) -> ToolPair:
    if done is None:
        return ToolPair(step, turn, position, call_id, tool, arguments, NO_RESULT, NO_RESULT)
    p = inflate(done.payload, blobs)
    value = p.get("value")
    recorded = value.get(VIEW_FIELD) if isinstance(value, Mapping) else None
    view = view_from_json(recorded) if isinstance(recorded, Mapping) else None
    listed = value.get(SPANS_FIELD) if isinstance(value, Mapping) else None
    cites = tuple(str(s) for s in listed) if isinstance(listed, list | tuple) else ()
    return ToolPair(
        step, turn, position, call_id, tool, arguments, str(p["outcome"]), p.get("code"), view,
        saved, reads, spans, cites,
    )  # fmt: skip
