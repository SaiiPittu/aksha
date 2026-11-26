"""A rig for the patent agent's episodes: a SQLite store, a fixture world and the real loop.

``PatentRig`` builds the agent with its tools reading one fixture world's ``sources/``
folder as the inputs source (the only folder a tool reads), drives runs through
``loop.drive.drive`` with the agent's rule-based model, and sends through the outbox to a
ledgered mock counsel mailbox. ``switch`` points the inputs at another fixture world and
records the change through ``apply_source_change``, with the folder as the versioned
source and the agent's own tools re-executing its recorded queries; ``restart`` closes the
store and opens it again, as a new process would. Graders read ``key.json``; nothing the
agent runs does.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from aksha import data
from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.sources.folder import FolderSource
from aksha.adapters.storage.sqlite import open_store
from aksha.agents.patent.agent import KINDS, build_agent
from aksha.agents.patent.fakes import PatentFake
from aksha.agents.patent.world import SourceWorld
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactKind, ArtifactVersion
from aksha.runtime.artifacts.versions import current
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.ids import LogIdSource
from aksha.runtime.execution.recovery import recover_on_startup, resume_owner
from aksha.runtime.grounding.types import ReturnedText
from aksha.runtime.interrupts.approvals import ApprovalTool, approval_authority, approvals
from aksha.runtime.interrupts.commands import answer, approve, handoff
from aksha.runtime.interrupts.reducers import interrupts
from aksha.runtime.invalidation.commands import accept_review, apply_source_change
from aksha.runtime.invalidation.reviews import open_reviews
from aksha.runtime.loop.drive import LoopDriver, drive, drive_rebuilds
from aksha.runtime.loop.types import DriveResult, LoopSetup
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.outbox.worker import OutboxWorker
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.session.types import CommandReceipt
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import Ok, ToolContext, ToolDecl
from tests.support.systems import Ledger

ROOT = Path(__file__).resolve().parents[2]
WORLDS = data.worlds()
AGENT_DIR = ROOT / "src" / "aksha" / "agents" / "patent"
OWNER = "o1"
AGENT = "patent"
RUN = "R-0001"
DEFINITION = "sha256:" + "7" * 64
TODAY = date(2026, 11, 26)
START = datetime(2026, 11, 26, 9, 0, 0, tzinfo=UTC)
REQUEST = "Assess the four invention candidates and prepare the chosen one for counsel."
GAP_ANSWER = "0.5 m from the vent edge, so its air flow stays outside the footprint."
FULL: Mapping[str, ArtifactKind] = {f"{AGENT}.{k.name}": k for k in KINDS}


def thaw(value: Any) -> Any:
    """Frozen content (mapping proxies and tuples) as plain dicts and lists."""
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(v) for v in value]
    return value


def key(world: str) -> dict[str, Any]:
    """The answer key of ``world`` (graders only)."""
    data: dict[str, Any] = json.loads((WORLDS / world / "key.json").read_text(encoding="utf-8"))
    return data


def sources(world: str) -> Path:
    return WORLDS / world / "sources"


class Inputs:
    """A fixture world's sources as the inputs source, re-pointable at another world."""

    name = "inputs"
    collections = ("input",)

    def __init__(self, world: str) -> None:
        self.folder = FolderSource("inputs", sources(world), "input")

    def use(self, world: str) -> None:
        self.folder = FolderSource("inputs", sources(world), "input")

    def read(self, source: str, record_id: str) -> ReturnedText | None:
        return self.folder.read(source, record_id)

    def records(self, source: str, collection: str) -> tuple[str, ...]:
        return self.folder.records(source, collection)

    def version(self, owner: str) -> str:
        return self.folder.version(owner)

    def record_version(self, owner: str, record_id: str) -> str | None:
        return self.folder.record_version(owner, record_id)


class PatentRig:
    """One owner's store, the patent agent over one world, and the loop's setup."""

    def __init__(self, path: Path, world: str = "stillroom") -> None:
        self.path = path
        self.clock = FakeClock(START)
        self.world = SourceWorld(TODAY)
        self.inputs = Inputs(world)
        self.world_name = world
        self.model = PatentFake()
        self.ledger = Ledger("counsel_mail")
        self.agent = build_agent(self.world)
        self._open()

    def _open(self) -> None:
        self.store: EventStore = open_store(self.path, self.clock, writer=True)
        listed = {t.spec.name: t for t in self.agent.tools if isinstance(t, ToolDecl)}
        self.setup = LoopSetup(
            store=self.store,
            owner=OWNER,
            agent=self.agent,
            tree=compile_tree(AGENT, self.agent.tasks),
            registry=ToolRegistry(self.agent, listed, ()),
            table=bind_aliases((), "fake", {"generate": self.model}),
            instructions=(AGENT_DIR / "prompts" / "instructions.md").read_text(encoding="utf-8"),
            request=REQUEST,
            artifacts=ArtifactStore(FULL),
            kinds=FULL,
            definition_hash=DEFINITION,
            context_policy=self.agent.context_policy,
            reader=self.inputs,
        )
        self.listed = listed
        self.send_tool = {"send_packet": ApprovalTool(listed["send_packet"].spec)}
        self.worker = self._worker()
        self.executor = StepExecutor(self.store, OWNER, self.clock, outbox=self.worker)
        self.ids = LogIdSource(self.store)

    def _worker(self) -> OutboxWorker:
        authority = approval_authority(self.send_tool, lambda entry: DEFINITION)
        return OutboxWorker(
            self.store, OWNER, self.clock, {"counsel_mail": self.ledger}, authority=authority
        )

    def close(self) -> None:
        self.store.close()

    # --- commands a person or the operator gives ----------------------------------------

    def start(self, run: str = RUN) -> str:
        payload = {
            "agent": AGENT,
            "cause": "request",
            "definition_hash": DEFINITION,
            "budget": {"model_turns": 400, "tool_calls": 800, "active_ms": 10**9},
        }
        self.store.append(OWNER, "U1", (NewEvent("run.started", payload, run=run),))
        return run

    def drive(self, run: str = RUN) -> DriveResult:
        return drive(self.setup, self.executor, run)

    def rebuild(self) -> tuple[DriveResult, ...]:
        return drive_rebuilds(self.setup, self.executor, self.ids)

    def choose(self, candidate: str) -> CommandReceipt:
        """The person answers the open selection question with ``candidate``."""
        (open_,) = [
            i for i in interrupts(self.view()).values() if i.state == "open" and i.kind == "choice"
        ]
        return answer(
            self.store, OWNER, open_.id, by="founder", text=candidate, tree=self.setup.tree
        )

    def pending_approval(self) -> Any:
        found = [a for a in approvals(self.view()).values() if a.state == "requested"]
        assert len(found) == 1, found
        return found[0]

    def approve(
        self, approval: str, signature: str, command_id: str | None = None
    ) -> CommandReceipt:
        return approve(self.store, OWNER, approval, signature, by="founder", command_id=command_id)

    def run_query(self, owner: str, query: str) -> str:
        """Re-execute a recorded query with the agent's own tool (``patent.<tool>?<args>``)."""
        head, _, raw = query.partition("?")
        decl = self.listed[head.partition(".")[2]]
        assert decl.body is not None
        ctx = ToolContext(owner, AGENT, "requery", None, "requery", 1, reader=self.inputs)
        result = decl.body(ctx, json.loads(raw) if raw else {}).result
        assert isinstance(result, Ok)
        return next(r.version for r in result.reads if r.kind == "query" and r.id == query)

    def change(self, root: Path) -> CommandReceipt:
        """Apply the demo's change set to a copy of the base world at ``root``; record it.

        The same two edits and one new publication ``make demo`` applies: a later
        consultant email date (and its history row) and family E's publication.
        """
        import shutil

        from aksha.interfaces.demo.world import apply_change

        shutil.copytree(sources(self.world_name), root)
        apply_change(root)
        self.inputs.folder = FolderSource("inputs", root, "input")
        return apply_source_change(
            self.store, OWNER, [self.inputs], "stillroom-v2", run_query=self.run_query
        )

    def switch(self, world: str) -> CommandReceipt:
        """Point the inputs at ``world`` and record the change set (one command)."""
        self.inputs.use(world)
        self.world_name = world
        return apply_source_change(
            self.store, OWNER, [self.inputs], f"switch:{world}", run_query=self.run_query
        )

    def accept(self, interrupt: str, card: str) -> CommandReceipt:
        return accept_review(self.store, OWNER, interrupt, card, self.ids, tools=self.send_tool)

    def handoff(self, approval: str) -> CommandReceipt:
        """The execution path for an approval granted outside a run (ADR-P17 D8), drained."""
        receipt = handoff(
            self.store, OWNER, approval, tools=self.send_tool, definition_hash=DEFINITION
        )
        if receipt.accepted:
            self.worker.drain()
        return receipt

    def restart(self) -> None:
        """A new process: the store opened again with a fresh executor and worker."""
        self.store.close()
        self._open()

    def recover(self) -> tuple[str, ...]:
        """Recovery on startup, then the lazy resume of the owner's in-flight runs."""
        loaded = {AGENT: DEFINITION}
        recover_on_startup(self.store, loaded, outbox=lambda o: self.worker, owners=(OWNER,))
        return resume_owner(
            self.store,
            OWNER,
            LoopDriver(self.setup),
            clock=self.clock,
            loaded=loaded,
            outbox=self.worker,
        )

    # --- reading the log ----------------------------------------------------------------

    def view(self) -> LogView:
        return load_view(self.store, OWNER)

    def events(self, type_: str | None = None, run: str | None = None) -> list[Event]:
        return [
            e
            for e in self.view().events
            if (type_ is None or e.type == type_) and (run is None or e.run == run)
        ]

    def current(self, kind: str, key_: str | None = None) -> ArtifactVersion | None:
        return current(self.view(), f"{AGENT}.{kind}", key_)

    def content(self, kind: str, key_: str | None = None) -> dict[str, Any]:
        """The current version's content as plain JSON data."""
        v = self.current(kind, key_)
        assert v is not None, (kind, key_)
        data: dict[str, Any] = thaw(v.content)
        return data

    def reviews(self) -> Mapping[str, Any]:
        return open_reviews(self.view())

    def open_interrupts(self, kind: str) -> list[Any]:
        return [i for i in interrupts(self.view()).values() if i.state == "open" and i.kind == kind]

    def answer_gap(self, text: str = GAP_ANSWER) -> CommandReceipt:
        """The founder relays the engineer-inventor's answer to the one open gap question."""
        (open_,) = self.open_interrupts("input")
        return answer(self.store, OWNER, open_.id, by="founder", text=text, tree=self.setup.tree)

    def run_to_selection(self) -> DriveResult:
        self.start()
        result = self.drive()
        assert result.state == "held", result
        if self.open_interrupts("input"):
            assert self.answer_gap().accepted
            result = self.drive()
            assert result.state == "held", result
        assert self.open_interrupts("choice"), result
        return result

    def run_to_approval(self, candidate: str = "C1") -> Any:
        self.run_to_selection()
        assert self.choose(candidate).accepted
        result = self.drive()
        assert result.state == "held", result
        return self.pending_approval()


# --- graders: they read key.json; nothing the agent runs does --------------------------

PATENT_FILES = ("notes/", "contributors/", "disclosures/", "priorities/", "publications/")


def norm(text: str) -> str:
    return " ".join(text.split())


def quotes(value: Any) -> list[tuple[str, str]]:
    """Every ``(file, quote)`` pair inside ``value`` (dicts that carry both)."""
    out: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        if isinstance(value.get("file"), str) and isinstance(value.get("quote"), str):
            out.append((value["file"], value["quote"]))
        for v in value.values():
            out += quotes(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            out += quotes(v)
    return out


def saved_contents(rig: PatentRig) -> list[Mapping[str, Any]]:
    """The content of every current version the agent saved."""
    out = []
    for kind in FULL:
        keys = {
            e.payload.get("key")
            for e in rig.events("artifact.saved")
            if e.payload.get("kind") == kind
        }
        for k in keys:
            v = current(rig.view(), kind, k)
            if v is not None:
                out.append(thaw(v.content))
    return out


def patent_facts(world: str) -> list[dict[str, Any]]:
    """The answer key's facts this agent is graded on (its files, not the research ones)."""
    return [f for f in key(world)["facts"] if f["file"].startswith(PATENT_FILES)]
