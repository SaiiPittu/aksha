"""What the research and patent adapters share: a runtime session driven through an episode.

:class:`RuntimeUnderTest` runs one agent on the Aksha runtime with its fake models, the way
the command line does (a ``Session``: the agent loop, the policy gate, the step executor,
the outbox), and plugs the episode in at the two places the world reaches the agent:

1. Reads. :class:`EpisodeReader` is the runtime's record reader (``RecordReader``) over the
   episode: every record a tool body reads is the episode's ``read_source``, and a
   collection is its ``list_sources``, so every read passes the path guard and the fault
   switches and is recorded as ``episode.tool_called``. The agent's own tools stay as they
   are, so the episode grades the agent that ships, not a stand-in with other tools. A
   fault switch's refusal reads as no record, which the tool reports as a failed fetch.
2. Sends. :class:`EpisodeMailbox` is the system of record the agent's effect names. When the
   run asks for an approval, the adapter first saves the outputs into the episode, then asks
   the episode's scripted user to approve the target, the action and the recipient, and
   only then grants the runtime approval; the outbox's ``apply`` then sends through the
   episode with that episode approval, so ``no_unbound_send`` sees one bound send.

Every answer a person would give is the adapter's ``answers`` (the case's scripted user).
The runtime store lives beside the world copy (``<episode>/runtime/``), never under
``sources/``. Calling :meth:`RuntimeUnderTest.run` again (a restart) opens that store
again and drives on from what it holds; saving unchanged outputs records nothing.

3. Changes. A change step that applied a change set to the world copy and restarted the
   agent (``episode.change_applied``) reaches the runtime the way a source change reaches
   any session: :class:`EpisodeSource` is the session's versioned source over the world
   copy, the adapter dispatches ``ApplySourceChange`` once per applied change set (its
   command ID names the episode event, so a second restart records nothing new), then
   drives the rebuild runs the change opened (``Session.drive_rebuilds``), answering
   them as it answers the request run.
4. Measures. :meth:`RuntimeUnderTest.metrics` reads the runtime store after the run:
   model turns, tool calls, tokens and context characters, split into the request runs
   and the rebuild runs, and each rebuild re-entry's mode and reason (decision 36). The
   runner records them on the episode's result row; no grader reads them.

The composition root (:mod:`aksha.app`) builds the session (store, clock, blobs, the loaded
agent bound to its fakes); this module never picks an adapter.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aksha.evals.episode import Episode, guard_path
from aksha.evals.errors import EvalError, PathRefused
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.grounding.types import ReturnedText
from aksha.runtime.outbox.types import SystemReceipt
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.session import named as n
from aksha.runtime.session.service import Session
from aksha.runtime.session.views import Waiting, run_statuses, session_status

OWNER = "evals"
SESSION = "S-eval"
MAX_ROUNDS = 50
"""More person turns than any case needs: a bound on a loop, never a run limit."""

SessionBuilder = Callable[[Path, "EpisodeReader", Mapping[str, Any], "EpisodeSource"], Session]
"""Builds the writer session over ``<folder>/``, with the reader, systems and source given."""


class EpisodeReader:
    """The runtime's record reader over one episode, for the source names it serves."""

    def __init__(self, episode: Episode, sources: tuple[str, ...]) -> None:
        """Serve ``sources`` (the agent's declared source names) from ``episode``."""
        self.episode = episode
        self.sources = sources
        self.texts: dict[str, str] = {}
        """Every record's text as last returned, by record ID (for citation offsets)."""

    def read(self, source: str, record_id: str) -> ReturnedText | None:
        """The record's text through ``read_source``; None for a refusal or a fault."""
        if source not in self.sources:
            return None
        try:
            text = self.episode.read_source(record_id)
        except EvalError:
            return None
        self.texts[record_id] = text
        return ReturnedText(source, record_id, _digest(text), text)

    def records(self, source: str, collection: str) -> tuple[str, ...]:
        """The record IDs under ``collection`` through ``list_sources``; empty on a refusal."""
        if source not in self.sources or not collection:
            return ()
        try:
            return tuple(self.episode.list_sources(collection))
        except EvalError:
            return ()

    def cite(self, record: str, quote: str) -> dict[str, Any]:
        """A bundle citation of ``quote`` in ``record``, with its offsets when it was read."""
        text = self.texts.get(record)
        if text is not None and quote and quote in text:
            at = text.index(quote)
            return {"file": record, "exact": quote, "start": at, "end": at + len(quote)}
        return {"file": record, "exact": quote}


class EpisodeSource:
    """The world copy as the session's versioned source (module doc, item 3).

    A record's version is ``sha256:`` over its text as the episode's copy now holds it, the
    same version :class:`EpisodeReader` returns for a read, so a file a change set replaced
    reads as moved. It is the runtime's source check, not an agent read: it opens the file
    behind the path guard and records no ``episode.tool_called``.
    """

    def __init__(self, episode: Episode, name: str, collections: tuple[str, ...]) -> None:
        """Version ``episode``'s copy of ``collections`` as source ``name``."""
        self.episode = episode
        self._name = name
        self._collections = collections

    @property
    def name(self) -> str:
        """The source's declared name."""
        return self._name

    @property
    def collections(self) -> tuple[str, ...]:
        """The collections it versions."""
        return self._collections

    def version(self, owner: str) -> str:
        """The whole copy's version: a hash over every record's version, by path."""
        root = self.episode.sources
        pairs = sorted(
            (p.relative_to(root).as_posix(), _digest(p.read_text(encoding="utf-8")))
            for c in self._collections
            if (root / c).is_dir()
            for p in (root / c).rglob("*")
            if p.is_file()
        )
        return _digest(repr(pairs))

    def record_version(self, owner: str, record_id: str) -> str | None:
        """The record's version now; None when the copy no longer holds it."""
        if record_id.split("/", 1)[0] not in self._collections:
            return None
        try:
            path = guard_path(self.episode.sources, record_id)
        except PathRefused:
            return None
        return _digest(path.read_text(encoding="utf-8")) if path.is_file() else None


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class EpisodeMailbox:
    """The system of record an effect names, delivering through the episode (module doc)."""

    name: str
    episode: Episode
    grants: dict[str, tuple[str, str, str, str]] = field(default_factory=dict)
    """Runtime approval target -> (episode approval, action, recipient, episode target)."""
    sent: dict[str, SystemReceipt] = field(default_factory=dict)

    def ledger_id(self) -> str:
        """Which ledger answers."""
        return f"{self.name}-episode"

    def apply(self, key: str, request: Mapping[str, Any]) -> SystemReceipt:
        """Send once per key, under the episode approval granted for the request's target.

        A key the episode's store already holds a delivery for (a send that landed before a
        restart) returns that delivery's receipt and sends nothing again.
        """
        landed = self.lookup(key, request)
        if landed is not None:
            return landed
        target = next((t for t in self.grants if t in _strings(request)), None)
        if target is None:
            raise EvalError("unbound_send", f"{self.name}: no episode approval for {key}")
        approval, action, recipient, ref = self.grants.pop(target)
        effect = self.episode.send(recipient, action, target_ref=ref, approval_id=approval,
                                   effect_key=key)  # fmt: skip
        self.sent[key] = SystemReceipt(key, self.ledger_id(), {"effect_key": effect})
        return self.sent[key]

    def lookup(self, key: str, request: Mapping[str, Any]) -> SystemReceipt | None:
        """The receipt of the send under ``key``, read from the episode's store; None if none."""
        if key in self.sent:
            return self.sent[key]
        for e in self.episode.store.read(self.episode.owner):
            if e.type == "episode.effect_sent" and e.payload.get("effect_key") == key:
                self.sent[key] = SystemReceipt(key, self.ledger_id(), {"effect_key": key})
                return self.sent[key]
        return None


def _strings(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return [value] if isinstance(value, str) else []


class RuntimeUnderTest:
    """One runtime agent put through an episode (module doc). Subclasses map its outputs."""

    name = ""
    mode = "fake"
    definition_hash = ""
    agent = ""
    sources: tuple[str, ...] = ()
    collections: tuple[str, ...] = ()
    """The collections of ``sources[0]`` a change set may move (:class:`EpisodeSource`)."""
    systems: tuple[str, ...] = ()
    request = ""

    def __init__(self, build: SessionBuilder, definition_hash: str = "") -> None:
        """Drive sessions that ``build`` makes (the composition root's).

        ``definition_hash`` is the hash of the definition ``build`` binds, when it differs
        from the class's (an arm that changes the context policy).
        """
        self.build = build
        if definition_hash:
            self.definition_hash = definition_hash
        self._metrics: dict[str, Any] = {}

    # --- the episode -------------------------------------------------------------------

    def run(self, episode: Episode) -> None:
        """Start (or continue) the run, answer as the scripted user, then save the outputs."""
        reader = EpisodeReader(episode, self.sources)
        boxes = {s: EpisodeMailbox(s, episode) for s in self.systems}
        source = EpisodeSource(episode, self.sources[0] if self.sources else "",
                               self.collections)  # fmt: skip
        session = self.build(episode.root.parent / "runtime", reader, boxes, source)
        try:
            self._drive(session, episode, reader, boxes)
            self._rebuild(session, episode, reader, boxes)
            view = load_view(session.store, OWNER)
            self._metrics = run_metrics(view)
            self.export(view, episode, reader, session.blobs)
        finally:
            session.store.close()

    def metrics(self) -> Mapping[str, Any]:
        """What the last :meth:`run` measured in the runtime store (module doc, item 4)."""
        return dict(self._metrics)

    def _rebuild(
        self,
        session: Session,
        episode: Episode,
        reader: EpisodeReader,
        boxes: Mapping[str, EpisodeMailbox],
    ) -> None:
        """Record each applied change set in the runtime, then settle the rebuilds."""
        applied = [
            (e.seq, str(e.payload["change_set"]))
            for e in episode.store.read(episode.owner)
            if e.type == "episode.change_applied"
        ]
        if not applied:
            return
        if not self.collections:
            raise EvalError("change_unsupported", f"{self.name}: no collections a change may move")
        for seq, name in applied:
            change = n.ApplySourceChange(
                owner=OWNER, change_set=name, command_id=f"episode-change-{seq}"
            )
            self._dispatch(session, change)
        for _ in range(MAX_ROUNDS):
            session.drive_rebuilds(OWNER)
            session.run_until_yield(OWNER)
            waiting = [
                w
                for r in run_statuses(load_view(session.store, OWNER))
                if r.state == "waiting"
                for w in r.waiting_on
            ]
            if not waiting:
                if not session.drive_rebuilds(OWNER):
                    return
                continue
            for w in waiting:
                self._respond(session, episode, reader, boxes, w)
        raise EvalError("rebuild_did_not_settle", f"{self.name}: rebuilds still open")

    def _drive(
        self,
        session: Session,
        episode: Episode,
        reader: EpisodeReader,
        boxes: Mapping[str, EpisodeMailbox],
    ) -> None:
        status = session_status(load_view(session.store, OWNER), SESSION)
        if status.run is None and not status.waiting_on:
            text = episode.opening or self.request
            self._dispatch(session, n.SendMessage(owner=OWNER, session=SESSION, text=text,
                                                  agent=self.agent))  # fmt: skip
        session.resume(OWNER)
        for _ in range(MAX_ROUNDS):
            session.run_until_yield(OWNER)
            status = session_status(load_view(session.store, OWNER), SESSION)
            if status.run is None or status.run.state != "waiting":
                return
            for w in status.waiting_on:
                self._respond(session, episode, reader, boxes, w)
        raise EvalError("run_did_not_end", f"{self.name}: no end after {MAX_ROUNDS} answers")

    def _respond(
        self,
        session: Session,
        episode: Episode,
        reader: EpisodeReader,
        boxes: Mapping[str, EpisodeMailbox],
        w: Waiting,
    ) -> None:
        if w.kind == "approval" and w.approval_id is not None and w.signature is not None:
            view = load_view(session.store, OWNER)
            self.export(view, episode, reader, session.blobs)
            granted = self.request_approval(view, episode, boxes, w)
            command: n.NamedCommand
            if granted:
                command = n.Approve(owner=OWNER, approval_id=w.approval_id,
                                    shown_signature=w.signature, by="scripted_user")  # fmt: skip
            else:
                command = n.Reject(owner=OWNER, approval_id=w.approval_id, by="scripted_user",
                                   note="refused by the scripted user")  # fmt: skip
            self._dispatch(session, command)
            return
        answers = {qid: self.answer(w, qid, text) for qid, text in w.questions}
        if w.kind == "choice" or len(answers) == 1:
            # The fallback is computed only when there is no answer: ``answer`` refuses.
            text = next(iter(answers.values())) if answers else self.answer(w, "", "")
            command = n.AnswerInterrupt(owner=OWNER, interrupt_id=w.interrupt_id, text=text,
                                        by="scripted_user")  # fmt: skip
        else:
            command = n.AnswerInterrupt(owner=OWNER, interrupt_id=w.interrupt_id,
                                        answers=answers, by="scripted_user")  # fmt: skip
        self._dispatch(session, command)

    @staticmethod
    def _dispatch(session: Session, command: n.NamedCommand) -> None:
        receipt = session.dispatch(command)
        if not receipt.accepted:
            raise EvalError("command_refused", f"{type(command).__name__}: {receipt.refused}")

    # --- what a subclass says --------------------------------------------------------

    def answer(self, w: Waiting, question: str, text: str) -> str:
        """The scripted user's answer to one question of interrupt ``w``."""
        raise EvalError("unanswerable", f"{self.name}: no scripted answer for {w.kind} "
                        f"question {question!r}: {text!r}")  # fmt: skip

    def request_approval(
        self, view: LogView, episode: Episode, boxes: Mapping[str, EpisodeMailbox], w: Waiting
    ) -> bool:
        """Ask the episode's scripted user for the approval ``w`` stands for; bind the send."""
        return False

    def export(
        self, view: LogView, episode: Episode, reader: EpisodeReader, blobs: BlobStore | None
    ) -> None:
        """Save the run's artifacts into the episode in the bundle schema."""
        raise NotImplementedError


def run_metrics(view: LogView) -> dict[str, Any]:
    """Turns, tool calls, tokens and re-entries, for request runs and rebuild runs.

    Counts come from the runtime's own facts: ``step.completed`` of model and tool steps
    (with ``usage``), ``step.started{context_chars}`` of model steps, and each rebuild
    re-entry's ``task.entered{reentry}``. A fake model that reports no usage counts zero
    tokens; ``context_chars`` is then the only size measure.
    """
    cause: dict[str, str] = {}
    for e in view.events:
        if e.type == "run.started" and e.run is not None:
            cause[e.run] = "rebuild" if e.payload.get("cause") == "rebuild" else "request"
    out: dict[str, Any] = {}
    for part in ("request", "rebuild"):
        out[part] = {"runs": sum(1 for c in cause.values() if c == part), "model_turns": 0,
                     "tool_calls": 0, "input_tokens": 0, "output_tokens": 0,
                     "context_chars": 0}  # fmt: skip
    modes: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for e in view.events:
        found = cause.get(e.run or "")
        if found is None:
            continue
        part, m = found, out[found]
        p = e.payload
        if e.type == "step.completed" and p.get("kind") == "model":
            m["model_turns"] += 1
            usage = p.get("usage") or {}
            m["input_tokens"] += int(usage.get("input_tokens", 0))
            m["output_tokens"] += int(usage.get("output_tokens", 0))
        elif e.type == "step.completed" and p.get("kind") == "tool":
            m["tool_calls"] += 1
        elif e.type == "step.started" and p.get("kind") == "model":
            m["context_chars"] += int(p.get("context_chars", 0))
        elif e.type == "task.entered" and part == "rebuild" and p.get("reentry"):
            choice = p["reentry"]
            mode = str(choice.get("mode", ""))
            modes[mode] = modes.get(mode, 0) + 1
            if choice.get("reason"):
                reasons[str(choice["reason"])] = reasons.get(str(choice["reason"]), 0) + 1
    out["rebuild"]["reentry_modes"] = dict(sorted(modes.items()))
    out["rebuild"]["fresh_reasons"] = dict(sorted(reasons.items()))
    return out
