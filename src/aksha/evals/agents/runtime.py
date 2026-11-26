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

The composition root (:mod:`aksha.app`) builds the session (store, clock, blobs, the loaded
agent bound to its fakes); this module never picks an adapter.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aksha.evals.episode import Episode
from aksha.evals.errors import EvalError
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.grounding.types import ReturnedText
from aksha.runtime.outbox.types import SystemReceipt
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.session import named as n
from aksha.runtime.session.service import Session
from aksha.runtime.session.views import Waiting, session_status

OWNER = "evals"
SESSION = "S-eval"
MAX_ROUNDS = 50
"""More person turns than any case needs: a bound on a loop, never a run limit."""

SessionBuilder = Callable[[Path, "EpisodeReader", Mapping[str, Any]], Session]
"""Builds the writer session over ``<folder>/``, with the reader and the systems given."""


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
        version = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        return ReturnedText(source, record_id, version, text)

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
        """Send once per key, under the episode approval granted for the request's target."""
        if key in self.sent:
            return self.sent[key]
        target = next((t for t in self.grants if t in _strings(request)), None)
        if target is None:
            raise EvalError("unbound_send", f"{self.name}: no episode approval for {key}")
        approval, action, recipient, ref = self.grants.pop(target)
        effect = self.episode.send(recipient, action, target_ref=ref, approval_id=approval,
                                   effect_key=key)  # fmt: skip
        self.sent[key] = SystemReceipt(key, self.ledger_id(), {"effect_key": effect})
        return self.sent[key]

    def lookup(self, key: str, request: Mapping[str, Any]) -> SystemReceipt | None:
        """The receipt of the send under ``key``, or None when it never landed."""
        return self.sent.get(key)


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
    systems: tuple[str, ...] = ()
    request = ""

    def __init__(self, build: SessionBuilder) -> None:
        """Drive sessions that ``build`` makes (the composition root's)."""
        self.build = build

    # --- the episode -------------------------------------------------------------------

    def run(self, episode: Episode) -> None:
        """Start (or continue) the run, answer as the scripted user, then save the outputs."""
        reader = EpisodeReader(episode, self.sources)
        boxes = {s: EpisodeMailbox(s, episode) for s in self.systems}
        session = self.build(episode.root.parent / "runtime", reader, boxes)
        try:
            self._drive(session, episode, reader, boxes)
            self.export(load_view(session.store, OWNER), episode, reader, session.blobs)
        finally:
            session.store.close()

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
            text = next(iter(answers.values()), self.answer(w, "", ""))
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
