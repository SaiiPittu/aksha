"""A pure fold of a session's stream events into transcript items (no Textual, no I/O).

:class:`Transcript` applies one event at a time (``apply`` returns the IDs of the items it
added or changed, so a view redraws only those) and :func:`fold` applies many. The same
events in the same order always give the same transcript, so a reconnect that replays
from a cursor and a full replay from zero end equal.

The items a chat shows:

- :class:`UserMessage`: ``user.message``.
- :class:`AgentTurn`: the model steps of one Task instance in a row, with one
  :class:`Line` per tool call (glyph, name, target, duration; an error or refusal with its
  code and reason) and per saved artifact, and the model's text with excerpt IDs numbered
  as citations ``[n]``.
- :class:`Question`: an ``input``, ``choice``, ``route``, ``review`` or ``budget`` interrupt.
- :class:`Approval`: the exact tool, facts, targets and signature, and its decision.
- :class:`Outcome`: ``run.ended``.
- :class:`Notice`: routing, uploads, changed inputs, rebuild runs, queued input, a stop
  request, a send through the outbox.
- :class:`Refusal`: a refused command the stream carries (``send_message``, ``steer``).

Events link to the session through their run (``run.started`` names it); the server's
stream already admits only this session's events and the owner-wide facts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

STEP = re.compile(r"^(?P<run>[^/]+)/(?P<inst>.+)/t(?P<turn>\d+)(?:/c(?P<call>\d+))?$")
EXCERPT = re.compile(r"\[?\b(EX-\d+)\b\]?")
TARGET_KEYS = (
    "path",
    "ref",
    "packet",
    "query",
    "url",
    "record",
    "id",
    "key",
    "kind",
    "recipient",
    "name",
    "text",
)
"""Which argument names a tool line shows as its target, first found first."""
TARGET_CHARS = 56

Tone = Literal["info", "ok", "warn", "bad", "run"]


@dataclass
class UserMessage:
    """A person's message."""

    id: str
    text: str
    agent: str | None = None
    kind: Literal["user"] = "user"


@dataclass
class Line:
    """One line of an agent turn: a tool call, or an artifact it saved."""

    step: str
    name: str
    target: str = ""
    status: Literal["running", "ok", "error", "saved"] = "running"
    code: str | None = None
    reason: str | None = None
    duration_ms: int | None = None


@dataclass
class Citation:
    """A numbered citation: the excerpt and the record it points into."""

    n: int
    span: str
    record: str | None


@dataclass
class AgentTurn:
    """The model turns of one Task instance in a row (module doc)."""

    id: str
    run: str
    agent: str | None
    instance: str
    turns: int = 0
    text: list[str] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    kind: Literal["turn"] = "turn"


@dataclass
class Question:
    """An interrupt a person answers in text or by picking an option."""

    id: str
    run: str | None
    qkind: str
    label: str
    questions: list[tuple[str, str]]
    options: list[str]
    state: Literal["open", "resolved", "voided"] = "open"
    answers: dict[str, str] = field(default_factory=dict)
    reason: str | None = None
    kind: Literal["question"] = "question"


@dataclass
class Approval:
    """An approval: exactly what would run, and the signature the decision binds to."""

    id: str
    run: str | None
    tool: str
    facts: list[tuple[str, str]]
    targets: list[str]
    signature: str
    interrupt: str | None = None
    canonical_args: str | None = None
    state: Literal["open", "granted", "rejected", "voided"] = "open"
    by: str | None = None
    note: str | None = None
    kind: Literal["approval"] = "approval"


@dataclass
class Outcome:
    """How a run ended."""

    id: str
    run: str
    agent: str | None
    outcome: str
    reason: str | None
    kind: Literal["outcome"] = "outcome"


@dataclass
class Notice:
    """A one-line fact in the thread."""

    id: str
    text: str
    tone: Tone = "info"
    glyph: str = "·"
    kind: Literal["notice"] = "notice"


@dataclass
class Refusal:
    """A command the runtime refused, with its code and message."""

    id: str
    code: str
    message: str
    command: str
    kind: Literal["refusal"] = "refusal"


Item = UserMessage | AgentTurn | Question | Approval | Outcome | Notice | Refusal


@dataclass
class RunInfo:
    """What the stream says of one run."""

    run: str
    agent: str | None
    session: str | None
    cause: str | None
    for_runs: tuple[str, ...] = ()
    state: Literal["running", "ended"] = "running"
    outcome: str | None = None


@dataclass
class ArtifactSlot:
    """One artifact slot (kind and key) and the versions this stream saw saved."""

    kind: str
    key: str | None
    versions: int = 0
    run: str | None = None

    @property
    def ref(self) -> str:
        """``kind[/key]@vN`` of the latest version seen."""
        return f"{self.kind}{'' if self.key is None else '/' + self.key}@v{self.versions}"


@dataclass
class Document:
    """An uploaded document's latest version as the stream told it."""

    doc_key: str
    path: str
    version: int
    media_type: str | None
    size: int | None


@dataclass
class Transcript:
    """The fold of one session's stream (module doc)."""

    session: str
    items: list[Item] = field(default_factory=list)
    by_id: dict[str, Item] = field(default_factory=dict)
    runs: dict[str, RunInfo] = field(default_factory=dict)
    artifacts: dict[tuple[str, str | None], ArtifactSlot] = field(default_factory=dict)
    documents: dict[str, Document] = field(default_factory=dict)
    cursor: int | None = None
    synchronized: bool = False
    title: str | None = None
    _steps: dict[str, str] = field(default_factory=dict)
    _calls: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    _spans: dict[str, str | None] = field(default_factory=dict)
    _cited: dict[str, int] = field(default_factory=dict)
    _upload: str | None = None
    _uploaded: list[str] = field(default_factory=list)
    _sends: dict[str, str] = field(default_factory=dict)
    _inputs: dict[str, str] = field(default_factory=dict)
    _n: int = 0

    # --- what a view asks -----------------------------------------------------------------

    @property
    def open_run(self) -> RunInfo | None:
        """The latest run of this session that has not ended."""
        for info in reversed(list(self.runs.values())):
            if info.state == "running" and info.cause != "rebuild":
                return info
        return None

    @property
    def last_run(self) -> RunInfo | None:
        """The latest run the stream named."""
        return next(reversed(list(self.runs.values())), None)

    def pending(self) -> list[Question | Approval]:
        """Questions and approvals still open, oldest first."""
        return [i for i in self.items if isinstance(i, Question | Approval) and i.state == "open"]

    def status(self) -> str:
        """``waiting``, ``running``, ``done``, ``failed``, ``stopped``, ``partial`` or ``idle``."""
        if self.pending():
            return "waiting"
        last = self.last_run
        if last is None:
            return "idle"
        if last.state == "running":
            return "running"
        return {"complete": "done", "blocked": "failed"}.get(
            last.outcome or "", last.outcome or "idle"
        )

    def citation(self, n: int) -> Citation | None:
        """The citation numbered ``n``."""
        for item in self.items:
            if isinstance(item, AgentTurn):
                for c in item.citations:
                    if c.n == n:
                        return c
        return None

    # --- the fold -------------------------------------------------------------------------

    def apply(self, event: Mapping[str, Any]) -> list[str]:
        """Apply one event (the ``data`` of an SSE event block); the item IDs it touched."""
        seq = event.get("seq")
        if isinstance(seq, int):
            if self.cursor is not None and seq <= self.cursor:
                return []
            self.cursor = seq
        kind = str(event.get("type") or "")
        payload: Mapping[str, Any] = event.get("payload") or {}
        handler = _HANDLERS.get(kind)
        if handler is None:
            return []
        touched = handler(self, event, payload)
        if kind != "document.stored" and touched:
            self._upload = None
        return touched

    def _add(self, item: Item) -> list[str]:
        self.items.append(item)
        self.by_id[item.id] = item
        return [item.id]

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"

    def _agent(self, run: str | None) -> str | None:
        info = self.runs.get(run or "")
        return None if info is None else info.agent

    # --- handlers -------------------------------------------------------------------------

    def _user(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        text = str(p.get("text") or "")
        if self.title is None:
            self.title = text[:80]
        return self._add(UserMessage(str(p.get("message") or self._id("M")), text, p.get("agent")))

    def _routed(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        agent = str(p.get("agent"))
        if p.get("by") == "classifier":
            ppm = dict(p.get("probabilities_ppm") or {}).get(agent)
            sure = "" if ppm is None else f" ({int(ppm) / 10_000:.0f}%)"
            text = f"routed to {agent} by the classifier{sure}"
        else:
            text = f"{agent} agent, as you asked"
        return self._add(Notice(self._id("route"), f"{text} · {p.get('run')}", "info", "→"))

    def _run_started(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        run = str(e.get("run") or p.get("run") or "")
        for_runs = tuple(str(r) for r in p.get("for_runs") or ())
        info = RunInfo(run, p.get("agent"), e.get("session"), p.get("cause"), for_runs)
        self.runs[run] = info
        if info.cause == "rebuild":
            behind = ", ".join(for_runs) or "earlier work"
            text = f"rebuilding {run} ({info.agent}) for {behind}: an input it read changed"
            return self._add(Notice(self._id("rebuild"), text, "run", "↻"))
        return []

    def _run_ended(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        run = str(e.get("run") or "")
        info = self.runs.setdefault(run, RunInfo(run, None, None, None))
        info.state = "ended"
        info.outcome = str(p.get("outcome") or "")
        out = Outcome(self._id("end"), run, info.agent, info.outcome, p.get("reason"))
        return self._add(out)

    def _turn_for(self, e: Mapping[str, Any], run: str, instance: str) -> tuple[AgentTurn, bool]:
        last = self.items[-1] if self.items else None
        if isinstance(last, AgentTurn) and last.run == run and last.instance == instance:
            return last, False
        turn = AgentTurn(self._id("turn"), run, self._agent(run), instance)
        self._add(turn)
        return turn, True

    def _step(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        step = str(e.get("step") or "")
        m = STEP.match(step)
        run = e.get("run")
        if m is None or not run:
            return []
        instance = m.group("inst")
        started = e.get("type") == "step.started"
        if p.get("kind") == "model":
            if started:
                return []
            turn, _ = self._turn_for(e, str(run), instance)
            turn.turns += 1
            self._steps[step] = turn.id
            result = (p.get("value") or {}).get("result") or {}
            for n, call in enumerate(result.get("tool_calls") or (), start=1):
                self._calls[f"{step}/c{n:02d}"] = call
            text = result.get("text")
            if text:
                turn.text.append(self._cite(turn, str(text)))
            return [turn.id]
        if p.get("kind") != "tool" or m.group("call") is None:
            return []
        model_step = step.rsplit("/", 1)[0]
        turn_id = self._steps.get(model_step)
        found = self.by_id.get(turn_id or "")
        if isinstance(found, AgentTurn):
            turn = found
        else:
            turn, _ = self._turn_for(e, str(run), instance)
            self._steps[model_step] = turn.id
        line = next((ln for ln in turn.lines if ln.step == step), None)
        name = str(p.get("tool") or p.get("name") or "tool")
        if line is None:
            call = self._calls.get(step) or {}
            line = Line(step, name, target_of(call.get("arguments")))
            turn.lines.append(line)
        if not started:
            line.duration_ms = p.get("duration_ms")
            if p.get("outcome") == "ok":
                line.status = "ok"
            else:
                line.status = "error"
                line.code = p.get("code") or str(p.get("outcome") or "error")
                line.reason = reason_of(p)
        return [turn.id]

    def _cite(self, turn: AgentTurn, text: str) -> str:
        def number(m: re.Match[str]) -> str:
            span = m.group(1)
            if span not in self._cited:
                self._cited[span] = len(self._cited) + 1
                turn.citations.append(Citation(self._cited[span], span, self._spans.get(span)))
            return f"[{self._cited[span]}]"

        return EXCERPT.sub(number, text)

    def _excerpts(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        for span in p.get("spans") or ():
            self._spans[str(span.get("span_id"))] = span.get("record_id")
        return []

    def _saved(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        kind = str(p.get("kind") or "")
        key = p.get("key")
        slot = self.artifacts.setdefault((kind, key), ArtifactSlot(kind, key))
        slot.versions += 1
        slot.run = e.get("run")
        run = e.get("run")
        if not run:
            return []
        turn, _ = self._turn_for(e, str(run), str(e.get("instance") or "run"))
        turn.lines.append(Line(f"save:{e.get('seq')}", "saved", slot.ref, "saved"))
        return [turn.id]

    def _raised(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        kind = str(p.get("kind") or "")
        if kind == "approval":
            return self._approval(e, p)
        iid = str(p.get("interrupt"))
        if iid in self.by_id:
            return []
        questions = [(str(q.get("id")), str(q.get("text"))) for q in p.get("questions") or ()]
        if kind == "review" and not questions:
            questions = [
                (
                    "review",
                    f"{p.get('old')} was rebuilt as {p.get('revision')}: "
                    "accept or reject the change",
                )
            ]
        options = [str(o) for o in p.get("options") or p.get("agents") or ()]
        label = str(p.get("label") or kind)
        return self._add(Question(iid, e.get("run"), kind, label, questions, options))

    def _approval(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        aid = str(p.get("approval") or p.get("interrupt"))
        body: Mapping[str, Any] = p.get("body") or {}
        facts = p.get("facts") if p.get("facts") is not None else body.get("facts")
        targets = p.get("targets") or [
            t[0] if isinstance(t, list | tuple) else t for t in body.get("targets") or ()
        ]
        existing = self.by_id.get(aid)
        if isinstance(existing, Approval):
            existing.canonical_args = existing.canonical_args or body.get("canonical_args")
            if p.get("interrupt"):
                existing.interrupt = str(p.get("interrupt"))
            return [aid]
        approval = Approval(
            aid,
            e.get("run"),
            str(p.get("tool") or body.get("tool") or ""),
            pairs(facts),
            [str(t) for t in targets],
            str(p.get("signature") or ""),
            interrupt=p.get("interrupt"),
            canonical_args=body.get("canonical_args"),
        )
        return self._add(approval)

    def _decided(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        kind = str(e.get("type"))
        item = self.by_id.get(str(p.get("approval") or p.get("interrupt") or ""))
        if isinstance(item, Approval):
            if kind in ("approval.granted",):
                item.state = "granted"
            elif kind in ("approval.rejected",):
                item.state = "rejected"
                item.note = p.get("note")
            elif kind in ("approval.voided", "interrupt.voided"):
                item.state = "voided"
            elif kind == "interrupt.resolved":
                decision = str(p.get("decision") or "")
                if decision.startswith("reject"):
                    item.state = "rejected"
                elif decision:
                    item.state = "granted"
            item.by = p.get("by") or item.by
            return [item.id]
        if isinstance(item, Question):
            if kind == "interrupt.voided":
                item.state = "voided"
                item.reason = p.get("reason")
            elif kind == "interrupt.resolved":
                item.state = "resolved"
                answers = p.get("answers") or {}
                if isinstance(answers, Mapping):
                    item.answers = {str(k): str(v) for k, v in answers.items()}
                if not item.answers and p.get("decision") is not None:
                    item.answers = {"decision": str(p.get("decision"))}
            return [item.id]
        return []

    def _queued(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        lane = str(p.get("lane") or "steer").replace("_", "-")
        nid = self._id("input")
        self._inputs[str(p.get("input_id"))] = nid
        return self._add(Notice(nid, f"{lane}: {p.get('text')} · queued", "info", "↳"))

    def _consumed(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        nid = self._inputs.get(str(p.get("input_id")))
        item = self.by_id.get(nid or "")
        if isinstance(item, Notice):
            item.text = item.text.replace(" · queued", " · read by the run")
            item.tone = "ok"
            return [item.id]
        return []

    def _stop_requested(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        reason = p.get("reason")
        text = f"stop requested for {e.get('run')}" + (f": {reason}" if reason else "")
        return self._add(Notice(self._id("stop"), text, "warn", "■"))

    def _refused(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        if e.get("session") != self.session:
            return []
        return self._add(
            Refusal(
                self._id("refused"),
                str(p.get("code")),
                str(p.get("message") or ""),
                str(p.get("name") or ""),
            )
        )

    def _document(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        path = str(p.get("path") or p.get("name") or "")
        self.documents[path] = Document(
            str(p.get("doc_key")),
            path,
            int(p.get("version") or 1),
            p.get("detected_type"),
            p.get("size_bytes"),
        )
        self._uploaded.append(path)
        if self._upload is not None and isinstance(self.by_id.get(self._upload), Notice):
            notice = self.by_id[self._upload]
            assert isinstance(notice, Notice)
            notice.text = upload_text(self._uploaded)
            return [notice.id]
        self._uploaded = [path]
        touched = self._add(Notice(self._id("upload"), upload_text(self._uploaded), "info", "▤"))
        self._upload = touched[0]
        return touched

    def _source_changed(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        records = p.get("records") or {}
        n = len(records)
        text = (
            f"inputs changed: {n} record{'s' if n != 1 else ''} moved; work that read them reopens"
        )
        return self._add(Notice(self._id("changed"), text, "warn", "↻"))

    def _outbox(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[str]:
        if e.get("type") == "outbox.enqueued":
            request = p.get("request") or {}
            what = ", ".join(f"{k} {v}" for k, v in dict(request).items())
            nid = self._id("send")
            self._sends[str(p.get("key"))] = nid
            text = f"{p.get('tool')} → {p.get('system')} ({what}) · leaves Aksha"
            return self._add(Notice(nid, text, "warn", "▲"))
        item = self.by_id.get(self._sends.get(str(p.get("key"))) or "")
        if isinstance(item, Notice) and e.get("type") == "outbox.completed":
            receipt = p.get("receipt") or {}
            got = ", ".join(f"{k} {v}" for k, v in dict(receipt).items())
            item.text = item.text.replace(" · leaves Aksha", f" · sent{': ' + got if got else ''}")
            item.tone = "ok"
            item.glyph = "✓"
            return [item.id]
        return []


def upload_text(paths: list[str]) -> str:
    """The upload notice: how many files, and the first few names."""
    shown = ", ".join(paths[:3]) + (f", +{len(paths) - 3} more" if len(paths) > 3 else "")
    return f"uploaded {len(paths)} file{'s' if len(paths) != 1 else ''}: {shown}"


def pairs(facts: object) -> list[tuple[str, str]]:
    """Facts as ``(name, value)`` pairs, from a mapping or a list of pairs, in given order."""
    if isinstance(facts, Mapping):
        return [(str(k), _plain(v)) for k, v in facts.items()]
    out: list[tuple[str, str]] = []
    for pair in facts if isinstance(facts, list | tuple) else ():
        if isinstance(pair, list | tuple) and len(pair) == 2:
            out.append((str(pair[0]), _plain(pair[1])))
    return out


def _plain(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def target_of(arguments: object) -> str:
    """The argument a tool line shows: the first of :data:`TARGET_KEYS`, else the first text."""
    if not isinstance(arguments, Mapping):
        return ""
    value: object = next((arguments[k] for k in TARGET_KEYS if arguments.get(k)), None)
    if value is None:
        value = next((v for v in arguments.values() if isinstance(v, str) and v), None)
    if value is None and arguments:
        value = json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"))
    text = "" if value is None else (value if isinstance(value, str) else json.dumps(value))
    text = " ".join(text.split())
    return text if len(text) <= TARGET_CHARS else text[: TARGET_CHARS - 1] + "…"


def reason_of(payload: Mapping[str, Any]) -> str | None:
    """A failed tool call's reason: the ``message`` of its recorded view, if any."""
    view = (payload.get("value") or {}).get("view") or {}
    text = view.get("text")
    if isinstance(text, str):
        try:
            data = json.loads(text)
        except ValueError:
            return text[:200]
        if isinstance(data, Mapping) and data.get("message"):
            return str(data["message"])
    return None


_HANDLERS = {
    "user.message": Transcript._user,
    "route.decided": Transcript._routed,
    "run.started": Transcript._run_started,
    "run.ended": Transcript._run_ended,
    "step.started": Transcript._step,
    "step.completed": Transcript._step,
    "excerpts.minted": Transcript._excerpts,
    "artifact.saved": Transcript._saved,
    "interrupt.raised": Transcript._raised,
    "approval.requested": Transcript._approval,
    "interrupt.resolved": Transcript._decided,
    "interrupt.voided": Transcript._decided,
    "approval.granted": Transcript._decided,
    "approval.rejected": Transcript._decided,
    "approval.voided": Transcript._decided,
    "input.queued": Transcript._queued,
    "input.consumed": Transcript._consumed,
    "run.stop_requested": Transcript._stop_requested,
    "command.refused": Transcript._refused,
    "document.stored": Transcript._document,
    "source.changed": Transcript._source_changed,
    "outbox.enqueued": Transcript._outbox,
    "outbox.completed": Transcript._outbox,
}


def fold(session: str, events: Iterable[Mapping[str, Any]]) -> Transcript:
    """The transcript of ``events``, applied in order."""
    transcript = Transcript(session)
    for event in events:
        transcript.apply(event)
    return transcript
