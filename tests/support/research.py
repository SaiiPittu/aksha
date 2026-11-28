"""A bench for the research agent: the loaded definition on a SQLite store over one world.

``ResearchBench`` loads ``src/aksha/agents/research`` through the real loader, opens a
store, and wires the loop's setup the way the composition root would: the rule-based
model (or any generation adapter), the folder of the chosen fixture world's ``sources/``
as the record reader (tools never see ``key.json``), the world's offline web behind
``web_search`` and ``fetch_url`` (its search proxy; ``web=None`` runs with no web), the
artifact store with the agent's kinds and relations. A model that takes the world's hints
(``for_world``) is given them, as the composition root does. ``restart`` closes the store
and opens it again with a fresh executor, as a new process after a kill would. ``key``
loads the world's answer key, for graders only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import aksha.agents
from aksha import data
from aksha.adapters.blobs.filesystem import FilesystemBlobStore
from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.search.world import WorldWeb, world_hints
from aksha.adapters.sources.folder import FolderSource
from aksha.adapters.storage.sqlite import open_store
from aksha.agents.research.fakes import ADAPTERS as FAKE_ADAPTERS
from aksha.app import world_hinted
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactVersion
from aksha.runtime.artifacts.versions import current
from aksha.runtime.budgets.types import Limits
from aksha.runtime.definitions.loader import LoadedDefinition, load_agents
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.recovery import resume_owner
from aksha.runtime.grounding.ports import RecordReader
from aksha.runtime.interrupts.commands import answer
from aksha.runtime.interrupts.reducers import open_interrupts
from aksha.runtime.loop.drive import LoopDriver, drive
from aksha.runtime.loop.types import DriveResult, LoopSetup
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.types import RunMode
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.sandbox.tier import ExecutionTier
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.tasks.tree import compile_tree
from tests.support.events import START

WORLDS = data.worlds()
AGENT_DIR = Path(aksha.agents.__file__).parent / "research"
OWNER = "founder-1"
RUN = "R-0001"
REQUEST = (
    "Who sells camera-free occupancy sensing for offices, and at what price? Compare power "
    "(PoE, Ethernet, mains, USB-C, battery powered, battery thermal, AA), battery life "
    "(battery life, years of battery, months at) and privacy (privacy, camera, faces, "
    "identities, image). What do customers say about: battery (batter*, not battery powered); "
    "stillness (sit*, sat, seated, still, do not move, barely move); setup (cabl*, "
    "electrician, socket*, wiring, install*, setup); privacy (privacy, works council, image)?"
)
"""The stillroom request: what to compare and what to read reviews for, in the request."""


def load() -> LoadedDefinition:
    """The research agent through the real loader."""
    (loaded,) = load_agents([AGENT_DIR], [])
    return loaded


def world_key(world: str) -> Mapping[str, Any]:
    """The world's answer key: graders read it, tools never do."""
    found: Mapping[str, Any] = json.loads((WORLDS / world / "key.json").read_text("utf-8"))
    return found


OPEN_BENCHES: set[ResearchBench] = set()
"""Benches whose store is open: closed after each test (``tests/agents/research/conftest.py``),
so no SQLite connection is left for the garbage collector to finalise inside a later test."""


def open_benches() -> set[ResearchBench]:
    """The benches whose store is open now."""
    return set(OPEN_BENCHES)


def close_benches_opened_since(before: set[ResearchBench]) -> None:
    """Close every bench opened after ``before`` was taken (a shared fixture's stays open)."""
    for bench in list(OPEN_BENCHES):
        if bench not in before:
            bench.close()


class ResearchBench:
    """One owner's store, the loaded research agent and the loop's setup over one world."""

    def __init__(
        self,
        tmp: Path,
        model: Any,
        *,
        world: str = "stillroom",
        request: str = REQUEST,
        reader: RecordReader | None = None,
        agent_changes: Mapping[str, Any] | None = None,
        mode: RunMode = "fake",
        web: WorldWeb | bool | None = True,
    ) -> None:
        self.tmp = tmp
        self.path = tmp / "aksha.sqlite"
        self.clock = FakeClock(START)
        hints = world_hints(WORLDS / world).get("research", {})
        self.model = world_hinted({"generate": model}, hints)["generate"]
        self.web = WorldWeb.of_world(WORLDS / world) if web is True else web or None
        self.mode = mode
        self.world = world
        self.loaded = load()
        self.agent = replace(self.loaded.agent, **(agent_changes or {}))
        self.reader: RecordReader = reader or FolderSource(
            "inputs", WORLDS / world / "sources", "input"
        )
        self.request = request
        self.blobs = FilesystemBlobStore(tmp / "blobs")
        self.store: EventStore = open_store(self.path, self.clock, writer=True)
        OPEN_BENCHES.add(self)
        self.setup = self._setup(self.store)
        self.executor = StepExecutor(self.store, OWNER, self.clock)

    def _setup(self, store: EventStore) -> LoopSetup:
        folder = AGENT_DIR
        agent = self.agent
        briefs = {
            t.id: (folder / t.brief).read_text(encoding="utf-8") for t in agent.tasks if t.brief
        }
        return LoopSetup(
            store=store,
            owner=OWNER,
            agent=agent,
            tree=compile_tree(agent.name, agent.tasks),
            registry=self.loaded.registry,
            table=bind_aliases(
                agent.model_aliases, self.mode, {**FAKE_ADAPTERS, "generate": self.model}
            ),
            instructions=(folder / agent.instructions).read_text(encoding="utf-8"),
            request=self.request,
            briefs=briefs,
            context_policy=agent.context_policy,
            artifacts=ArtifactStore(self.loaded.kinds, self.loaded.relations, blobs=self.blobs),
            kinds=self.loaded.kinds,
            definition_hash=self.loaded.definition_hash,
            limits=agent.limits or Limits(),
            blobs=self.blobs,
            reader=self.reader,
            tier=ExecutionTier(search=self.web, web=self.web),
        )

    def start(self) -> None:
        budget = self.agent.budget
        assert budget is not None
        payload = {
            "agent": self.agent.name, "cause": "request",
            "definition_hash": self.loaded.definition_hash,
            "budget": {"model_turns": budget.model_turns, "tool_calls": budget.tool_calls,
                       "active_ms": budget.active_ms},
            "invalidation_keys": self.agent.invalidation_keys,
        }  # fmt: skip
        self.store.append(OWNER, "U1", (NewEvent("run.started", payload, run=RUN),))

    def drive(self) -> DriveResult:
        return drive(self.setup, self.executor, RUN)

    def answer_open(self, **answers: str) -> None:
        """Answer the one open input interrupt with ``answers`` by question ID."""
        (i,) = [i for i in open_interrupts(self.view(), RUN) if i.kind == "input"]
        receipt = answer(self.store, OWNER, i.id, by="founder", answers=answers,
                         tree=self.setup.tree)  # fmt: skip
        assert receipt.refused is None, receipt.refused

    def run_to_end(self, **answers: str) -> DriveResult:
        """Drive, answer each question the run asks from ``answers``, until it ends."""
        result = self.drive()
        for _ in range(5):
            if result.state == "ended":
                return result
            asked = [i for i in open_interrupts(self.view(), RUN) if i.kind == "input"]
            assert asked, f"held with no question: {result}"
            ids = [str(q["id"]) for q in asked[0].payload.get("questions", ())]
            self.answer_open(**{q: answers[q] for q in ids})
            result = self.drive()
        return result

    def restart(self) -> None:
        """A new process: the store opened again with a fresh executor."""
        self.store.close()
        self.store = open_store(self.path, self.clock, writer=True)
        self.setup = self._setup(self.store)
        self.executor = StepExecutor(self.store, OWNER, self.clock)

    def resume(self) -> tuple[str, ...]:
        return resume_owner(
            self.store, OWNER, LoopDriver(self.setup), clock=self.clock,
            loaded={self.agent.name: self.loaded.definition_hash},
        )  # fmt: skip

    def view(self) -> LogView:
        return load_view(self.store, OWNER)

    def events(self, type_: str | None = None) -> list[Event]:
        return [e for e in self.view().events if type_ is None or e.type == type_]

    def latest(self, kind: str, key: str | None = None) -> ArtifactVersion | None:
        return current(self.view(), f"research.{kind}", key, blobs=self.blobs)

    def content(self, kind: str, key: str | None = None) -> Mapping[str, Any]:
        found = self.latest(kind, key)
        assert found is not None, f"no {kind} {key or ''}"
        return found.content

    def ended(self) -> Mapping[str, Any]:
        (end,) = self.events("run.ended")
        return end.payload

    def close(self) -> None:
        OPEN_BENCHES.discard(self)
        self.store.close()
