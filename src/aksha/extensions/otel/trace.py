"""``build_trace``: a run's trace as a pure projection of its stored events (ADR-P21 D1, D2, D6).

No OpenTelemetry package is imported here: the result is plain :class:`SpanRecord` values,
which ``export.py`` hands to the SDK. Replaying or resuming a step writes no event, so it
cannot add a span: one span per logical step holds by construction.

1. Segments. A run's events split into execution segments at a run-level HOLD (the run
   suspends for a person) and wherever the process that wrote them changed (``boot``). Each
   segment is one trace; its root links ``resumed_from`` to the previous segment's root,
   and every root carries ``gen_ai.conversation.id`` (the session), so backends group them.
2. The tree: run > Task instance > turn > {model call, tool call}; a stop-policy
   evaluation sits under its instance (or the run); an interrupt under the turn whose step
   raised it. A model or tool span covers all attempts, one ``aksha.attempt`` event each.
   An interrupt still open when its segment ends ends there with ``aksha.interrupt.state
   = open``; its answer in a later segment is an ``aksha.interrupt.resolved`` event.
3. Every other mapped event is a span event on the innermost span holding it; an event
   type with no row in ``semconv.EVENT_MAP`` becomes ``aksha.unmapped {type}`` (D9).
4. IDs are derived (D6): ``trace_id = sha256("aksha|" + store_id + "|" + run + "|" +
   segment)[:32]`` and ``span_id = sha256(trace_id + "|" + key)[:16]``, so two exports of
   one run are byte-identical and a re-export overwrites rather than duplicates.
5. Content (D5): under ``NO_CONTENT`` (the default) no tool result text leaves; the span
   modes add ``gen_ai.tool.call.result``, the event modes a span event holding it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from aksha.extensions.otel import semconv as sc
from aksha.runtime.api import Event

Status = Literal["UNSET", "OK", "ERROR"]
AttrValue = str | bool | int | float

_TURN = re.compile(r"^(?P<run>[^/]+)/(?P<instance>.+?)/t(?P<turn>[0-9]+)(?:/.*)?$")
_ERROR_OUTCOMES = frozenset({"error", "refused", "effects_unknown"})


@dataclass(frozen=True)
class SpanEvent:
    """A fact with no duration on a span."""

    name: str
    at_ns: int
    attributes: Mapping[str, AttrValue] = field(default_factory=dict)


@dataclass(frozen=True)
class SpanLink:
    """A link to a span of another trace."""

    trace_id: str
    span_id: str
    attributes: Mapping[str, AttrValue] = field(default_factory=dict)


@dataclass(frozen=True)
class SpanRecord:
    """One span: derived IDs, recorded times, attributes, events, links and status."""

    key: str
    name: str
    trace_id: str
    span_id: str
    parent_id: str | None
    start_ns: int
    end_ns: int
    attributes: Mapping[str, AttrValue]
    events: tuple[SpanEvent, ...] = ()
    links: tuple[SpanLink, ...] = ()
    status: Status = "UNSET"


@dataclass(frozen=True)
class Segment:
    """One execution segment of a run: one trace."""

    run: str
    index: int
    trace_id: str
    outcome: Literal["ended", "suspended", "open"]
    spans: tuple[SpanRecord, ...]


def trace_id(store_id: str, run: str, segment: int) -> str:
    """The derived trace ID of one segment (D6)."""
    return hashlib.sha256(f"aksha|{store_id}|{run}|{segment}".encode()).hexdigest()[:32]


def span_id(trace: str, key: str) -> str:
    """The derived span ID of ``key`` in ``trace`` (D6)."""
    return hashlib.sha256(f"{trace}|{key}".encode()).hexdigest()[:16]


def at_ns(at: str) -> int:
    """An event's recorded time in nanoseconds since the epoch."""
    moment = datetime.fromisoformat(at.replace("Z", "+00:00"))
    return int(moment.timestamp()) * 1_000_000_000 + moment.microsecond * 1_000


def segments_of(events: Sequence[Event]) -> list[list[Event]]:
    """The run's events split into execution segments (module doc, item 1)."""
    out: list[list[Event]] = []
    current: list[Event] = []
    for e in events:
        if current and (e.boot != current[-1].boot or _suspends(current[-1])):
            out.append(current)
            current = []
        current.append(e)
    if current:
        out.append(current)
    return out


def _suspends(e: Event) -> bool:
    return e.type == "stop.decided" and e.payload.get("verdict") == "hold" and e.instance is None


class _Builder:
    """The spans of one segment, built in one pass over its events."""

    def __init__(self, trace: str, index: int, capture: str) -> None:
        self.trace = trace
        self.index = index
        self.capture = capture
        self.spans: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []

    def open(self, key: str, name: str, parent: str | None, start: int,
             attributes: Mapping[str, AttrValue]) -> dict[str, Any]:  # fmt: skip
        if key not in self.spans:
            self.spans[key] = {"key": key, "name": name, "parent": parent, "start": start,
                               "end": start, "attributes": dict(attributes), "events": [],
                               "links": [], "status": "UNSET"}  # fmt: skip
            self.order.append(key)
        return self.spans[key]

    def touch(self, key: str, at: int) -> None:
        span = self.spans.get(key)
        while span is not None:
            span["end"] = max(span["end"], at)
            span["start"] = min(span["start"], at)
            span = self.spans.get(span["parent"]) if span["parent"] else None

    def event(self, key: str, name: str, at: int, attributes: Mapping[str, AttrValue]) -> None:
        self.spans[key]["events"].append(SpanEvent(name, at, dict(attributes)))
        self.touch(key, at)

    def records(self) -> tuple[SpanRecord, ...]:
        out = []
        for key in self.order:
            s = self.spans[key]
            parent = None if s["parent"] is None else span_id(self.trace, s["parent"])
            out.append(SpanRecord(
                key=key, name=s["name"], trace_id=self.trace, span_id=span_id(self.trace, key),
                parent_id=parent, start_ns=s["start"], end_ns=s["end"],
                attributes=dict(sorted(s["attributes"].items())), events=tuple(s["events"]),
                links=tuple(s["links"]), status=s["status"],
            ))  # fmt: skip
        return tuple(out)


def build_trace(
    events: Sequence[Event], run: str, *, store_id: str, capture: str = sc.CAPTURE_DEFAULT
) -> tuple[Segment, ...]:
    """The trace of ``run``, one :class:`Segment` per execution segment (module doc). Pure."""
    mine = sorted((e for e in events if e.run == run), key=lambda e: e.seq)
    started = next((e for e in mine if e.type == "run.started"), None)
    if started is None:
        return ()
    agent = str(started.payload.get("agent", ""))
    session = started.session
    fake = started.payload.get("run_mode") in ("fake", "scripted", "replay")
    out: list[Segment] = []
    previous_root: tuple[str, str] | None = None
    raised_in: dict[str, str] = {}
    for index, part in enumerate(segments_of(mine), start=1):
        trace = trace_id(store_id, run, index)
        b = _Builder(trace, index, capture)
        first = at_ns(part[0].at)
        root_attrs: dict[str, AttrValue] = {
            sc.GEN_AI_OPERATION: sc.OP_WORKFLOW, sc.GEN_AI_AGENT_NAME: agent,
            sc.AKSHA_RUN: run, sc.AKSHA_SEGMENT: index,
        }  # fmt: skip
        if session:
            root_attrs[sc.GEN_AI_CONVERSATION] = session
        if started.payload.get("definition_hash"):
            root_attrs[sc.AKSHA_AGENT_VERSION] = str(started.payload["definition_hash"])
        if started.payload.get("headless") is True:
            root_attrs[sc.AKSHA_HEADLESS] = True
        root = b.open("run", sc.RUN_SPAN.format(agent=agent), None, first, root_attrs)
        if previous_root is not None:
            root["links"].append(SpanLink(*previous_root, {sc.AKSHA_LINK: sc.LINK_RESUMED_FROM}))
        for e in part:
            _apply(b, e, agent, fake, raised_in)
        outcome: Literal["ended", "suspended", "open"]
        if any(e.type == "run.ended" for e in part):
            outcome = "ended"
        elif _suspends(part[-1]):
            outcome = "suspended"
        else:
            outcome = "open"
        root["attributes"][sc.AKSHA_SEGMENT_OUTCOME] = outcome
        b.touch("run", at_ns(part[-1].at))
        for key, span in b.spans.items():
            if key.startswith("interrupt:") and sc.AKSHA_INTERRUPT_STATE not in span["attributes"]:
                span["attributes"][sc.AKSHA_INTERRUPT_STATE] = "open"
        out.append(Segment(run, index, trace, outcome, b.records()))
        previous_root = (trace, span_id(trace, "run"))
    return tuple(out)


def _task_key(b: _Builder, e: Event, agent: str, at: int) -> str:
    if e.instance is None:
        return "run"
    key = f"task:{e.instance}"
    attrs = {sc.GEN_AI_OPERATION: sc.OP_AGENT, sc.GEN_AI_AGENT_NAME: agent,
             sc.AKSHA_TASK_ID: e.instance}  # fmt: skip
    b.open(key, sc.TASK_SPAN.format(agent=agent), "run", at, attrs)
    return key


def _turn_key(b: _Builder, e: Event, agent: str, at: int) -> str:
    """The turn span of ``e``'s step, opening its Task and turn spans; else the Task's."""
    match = _TURN.match(e.step or "")
    if match is None:
        return _task_key(b, e, agent, at)
    instance, turn = match.group("instance"), int(match.group("turn"))
    task = f"task:{instance}"
    b.open(task, sc.TASK_SPAN.format(agent=agent), "run", at,
           {sc.GEN_AI_OPERATION: sc.OP_AGENT, sc.GEN_AI_AGENT_NAME: agent,
            sc.AKSHA_TASK_ID: instance})  # fmt: skip
    key = f"turn:{instance}/t{turn:02d}"
    b.open(key, sc.TURN_SPAN.format(instance=instance, turn=turn), task, at,
           {sc.AKSHA_TURN: turn})  # fmt: skip
    return key


def _innermost(b: _Builder, e: Event, agent: str, at: int) -> str:
    if e.step is not None and f"step:{e.step}" in b.spans:
        return f"step:{e.step}"
    if e.step is not None:
        return _turn_key(b, e, agent, at)
    return _task_key(b, e, agent, at)


def _apply(b: _Builder, e: Event, agent: str, fake: bool, raised_in: dict[str, str]) -> None:
    at = at_ns(e.at)
    p = e.payload
    if e.type in ("run.started", "run.ended"):
        if e.type == "run.ended":
            b.spans["run"]["attributes"][sc.AKSHA_RUN_OUTCOME] = str(p.get("outcome"))
            b.spans["run"]["status"] = "OK" if p.get("outcome") == "complete" else "UNSET"
        b.touch("run", at)
        return
    if e.type == "task.entered":
        b.touch(_task_key(b, e, agent, at), at)
        return
    if e.type == "step.started" and e.step is not None:
        _step_started(b, e, agent, fake, at)
        return
    if e.type in ("step.completed", "step.failed") and e.step is not None:
        _step_ended(b, e, agent, at)
        return
    if e.type == "stop.decided":
        parent = _task_key(b, e, agent, at)
        scope = e.instance or e.run or ""
        attrs: dict[str, AttrValue] = {sc.AKSHA_STOP_VERDICT: str(p.get("verdict"))}
        if p.get("cause"):
            attrs[sc.AKSHA_STOP_CAUSE] = str(p["cause"])
        if p.get("reason"):
            attrs[sc.AKSHA_STOP_REASON] = str(p["reason"])
        b.open(f"stop:{e.seq}", sc.STOP_SPAN.format(scope=scope), parent, at, attrs)
        b.touch(f"stop:{e.seq}", at)
        return
    if e.type == "interrupt.raised":
        ident = str(p.get("interrupt"))
        parent = _innermost(b, e, agent, at)
        if parent.startswith("step:"):
            parent = b.spans[parent]["parent"] or parent
        attrs = {sc.AKSHA_INTERRUPT_ID: ident, sc.AKSHA_INTERRUPT_KIND: str(p.get("kind"))}
        if p.get("signature"):
            attrs[sc.AKSHA_APPROVAL_SIGNATURE] = str(p["signature"])
        b.open(f"interrupt:{ident}", sc.INTERRUPT_SPAN.format(kind=p.get("kind")), parent, at,
               attrs)  # fmt: skip
        raised_in[ident] = b.trace
        return
    if e.type in ("interrupt.resolved", "interrupt.voided"):
        ident = str(p.get("interrupt"))
        key = f"interrupt:{ident}"
        state = "resolved" if e.type == "interrupt.resolved" else "voided"
        if key in b.spans:
            b.spans[key]["attributes"][sc.AKSHA_INTERRUPT_STATE] = state
            b.touch(key, at)
        else:
            b.event("run", sc.EV_INTERRUPT_RESOLVED, at,
                    {sc.AKSHA_INTERRUPT_ID: ident, sc.AKSHA_INTERRUPT_STATE: state})  # fmt: skip
        return
    row = sc.EVENT_MAP.get(e.type)
    if row is None:
        b.event(_innermost(b, e, agent, at), sc.EV_UNMAPPED.format(type=e.type), at,
                {sc.AKSHA_EVENT_TYPE: e.type})  # fmt: skip
        return
    role, name = row
    target = _innermost(b, e, agent, at)
    if role == "attribute":
        if e.type == "context.compacted":
            b.spans[target]["attributes"][sc.GEN_AI_COMPACTED] = True
        b.touch(target, at)
        return
    attrs = {sc.AKSHA_EVENT_TYPE: e.type}
    if e.type == "artifact.saved" and p.get("ref"):
        attrs[sc.AKSHA_ARTIFACT_REF] = str(p["ref"])
    b.event(target, name, at, attrs)


def _step_started(b: _Builder, e: Event, agent: str, fake: bool, at: int) -> None:
    p = e.payload
    turn = _turn_key(b, e, agent, at)
    key = f"step:{e.step}"
    if key in b.spans:  # a retry or a resumed attempt: the same logical span
        b.event(key, sc.EV_ATTEMPT, at, {sc.AKSHA_ATTEMPT_N: int(p.get("attempt") or 0)})
        return
    attrs: dict[str, AttrValue] = {sc.AKSHA_STEP: str(e.step)}
    if p.get("kind") == "model":
        model_class = str(p.get("model_class") or "generation")
        operation = sc.MODEL_OPERATIONS.get(model_class, sc.OP_CHAT)
        if p.get("native") is False:
            operation = sc.OP_CHAT
        model = str(p.get("adapter") or p.get("alias") or "model")
        attrs.update({sc.GEN_AI_OPERATION: operation, sc.GEN_AI_REQUEST_MODEL: model,
                      sc.AKSHA_MODEL_CLASS: model_class})  # fmt: skip
        if p.get("alias"):
            attrs[sc.AKSHA_MODEL_ALIAS] = str(p["alias"])
        if p.get("native") is not None:
            attrs[sc.AKSHA_MODEL_NATIVE] = bool(p["native"])
        if fake:
            attrs[sc.GEN_AI_PROVIDER] = sc.PROVIDER_FAKE
        name = sc.MODEL_SPAN.format(operation=operation, model=model)
    else:
        tool = str(p.get("tool") or p.get("name") or "tool")
        attrs.update({sc.GEN_AI_OPERATION: sc.OP_TOOL, sc.GEN_AI_TOOL_NAME: tool,
                      sc.GEN_AI_TOOL_CALL_ID: str(e.step)})  # fmt: skip
        name = sc.TOOL_SPAN.format(tool=tool)
    b.open(key, name, turn, at, attrs)
    b.touch(key, at)


def _step_ended(b: _Builder, e: Event, agent: str, at: int) -> None:
    key = f"step:{e.step}"
    if key not in b.spans:
        b.event(_turn_key(b, e, agent, at), sc.EV_UNMAPPED.format(type=e.type), at,
                {sc.AKSHA_EVENT_TYPE: e.type})  # fmt: skip
        return
    p = e.payload
    span = b.spans[key]
    outcome = "error" if e.type == "step.failed" else str(p.get("outcome"))
    span["attributes"][sc.AKSHA_STEP_OUTCOME] = outcome
    attempts = int(p.get("attempts") or 1)
    if attempts > 1:
        span["attributes"][sc.AKSHA_RETRY_COUNT] = attempts - 1
    usage = p.get("usage")
    if isinstance(usage, Mapping):
        if "input_tokens" in usage:
            span["attributes"][sc.GEN_AI_INPUT_TOKENS] = int(usage["input_tokens"])
        if "output_tokens" in usage:
            span["attributes"][sc.GEN_AI_OUTPUT_TOKENS] = int(usage["output_tokens"])
    if outcome in _ERROR_OUTCOMES:
        span["status"] = "ERROR"
        if outcome == "refused":
            span["attributes"][sc.ERROR_TYPE] = sc.REFUSED_ERROR
            if p.get("layer"):
                span["attributes"][sc.AKSHA_POLICY_RULE] = str(p["layer"])
        else:
            span["attributes"][sc.ERROR_TYPE] = str(p.get("code") or outcome)
    elif outcome == "ok":
        span["status"] = "OK"
    view = p.get("value", {}).get("view") if isinstance(p.get("value"), Mapping) else None
    text = view.get("text") if isinstance(view, Mapping) else None
    if isinstance(text, str) and b.capture in ("SPAN_ONLY", "SPAN_AND_EVENT"):
        span["attributes"][sc.GEN_AI_TOOL_RESULT] = text
    if isinstance(text, str) and b.capture in ("EVENT_ONLY", "SPAN_AND_EVENT"):
        span["events"].append(SpanEvent(sc.GEN_AI_TOOL_RESULT, at, {"content": text}))
    b.touch(key, at)
