"""A bench for memory through the agent loop (ADR-P09): one agent, many runs, one store.

The agent ``scout`` has one intake Task, ``scope``, that may ask a person, saves a
``scope`` artifact and has memory applied: ``recall`` is its forced first call, and it may
``remember`` and ``forget``. ``Bench.run`` starts a run and drives it with a scripted
model, answering the run's one question when it is held, so a test can play the three
runs of the ADR's sequence diagram on one SQLite store.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.extensions.memory import MemorySpec, memory_extension
from aksha.extensions.memory.kinds import FACT, PREFERENCE, PROCEDURE
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactKind
from aksha.runtime.definitions.types import Agent
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.interrupts.commands import answer
from aksha.runtime.interrupts.reducers import open_interrupts
from aksha.runtime.interrupts.types import DeclaredInterrupt
from aksha.runtime.invalidation.view import InvalidationView, invalidation_view
from aksha.runtime.loop.drive import drive
from aksha.runtime.loop.types import DriveResult, LoopSetup
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.rules import RuleGenerationModel
from aksha.runtime.models.types import GenerationRequest, GenerationResult, ToolCall
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import CheckResult, Task
from aksha.runtime.tasks.view import TaskView
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import ToolDecl, ToolUse
from tests.support.events import START

OWNER = "o"
AGENT = "scout"
DEFINITION = "sha256:" + "b" * 64
BUDGET = {"model_turns": 1_000, "tool_calls": 1_000, "active_ms": 10**9}
SCOPE = ArtifactKind("scope", {"type": "object"})
SPEC = MemorySpec(subjects=("research.geography",), intake_tasks=("scope",))
GEO = "memory.preference/research.geography"

Reply = Callable[[int, GenerationRequest], list[ToolCall]]
"""A run's script: the calls of turn ``n`` (1-based, after the forced recall)."""


def scoped_this_run(view: TaskView) -> CheckResult:
    """The intake's check: this run saved a scope."""
    found = view.latest("scope")
    return CheckResult(found is not None and found.run == view.instance.run, "save the scope")


def agent() -> Agent:
    """``scout`` with memory applied."""
    base = Agent(
        name=AGENT,
        purpose="Scope a vendor search.",
        instructions="prompts/instructions.md",
        reads=(),
        artifacts=(SCOPE,),
        tools=(ToolUse("save_artifact"),),
        tasks=(
            Task("scope", tools=("save_artifact",), writes=("scope",), done=scoped_this_run,
                 interrupt=DeclaredInterrupt("input", "question")),
        ),
    )  # fmt: skip
    return memory_extension(SPEC, AGENT).apply(base)


def call(name: str, n: int = 1, **args: Any) -> ToolCall:
    return ToolCall(f"call-{name}-{n}", name, args)


def statement(request: GenerationRequest) -> str:
    """The newest statement span the context tells the model to cite."""
    found = re.findall(r"cite this answer as statement (EX-\d{4})", _text(request))
    assert found, "no statement in the context"
    return str(found[-1])


def _text(request: GenerationRequest) -> str:
    return "\n".join(m.text or "" for m in request.messages)


class Scripted(RuleGenerationModel):
    """Calls ``recall`` while it is forced; otherwise the current run's script by turn."""

    def __init__(self) -> None:
        self.script: Reply = lambda n, r: [call("finish", summary="done")]
        self.turn = 0
        self.requests: list[GenerationRequest] = []

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        if request.tool_choice == "recall":
            return GenerationResult(None, (call("recall", query="vendor geography regions"),),
                                    "tool_calls")  # fmt: skip
        self.turn += 1
        return GenerationResult(None, tuple(self.script(self.turn, request)), "tool_calls")


class Bench:
    """One owner's SQLite store, the ``scout`` agent and the loop's setup over them."""

    def __init__(self, path: Path) -> None:
        self.clock = FakeClock(START)
        self.model = Scripted()
        self.agent = agent()
        self.store = open_store(path, self.clock, writer=True)
        listed = {t.spec.name: t for t in self.agent.tools if isinstance(t, ToolDecl)}
        kinds = {
            f"{AGENT}.scope": SCOPE,
            "memory.preference": PREFERENCE,
            "memory.fact": FACT,
            "memory.procedure": PROCEDURE,
        }
        self.setup = LoopSetup(
            store=self.store, owner=OWNER, agent=self.agent,
            tree=compile_tree(AGENT, self.agent.tasks),
            registry=ToolRegistry(self.agent, listed, ()),
            table=bind_aliases((), "fake", {"generate": self.model}),
            instructions="You scope vendor searches.", request="Find sensor vendors.",
            artifacts=ArtifactStore(kinds), kinds=kinds,
            context_policy=self.agent.context_policy, definition_hash=DEFINITION,
        )  # fmt: skip
        self.executor = StepExecutor(self.store, OWNER, self.clock)

    def run(self, run: str, script: Reply, *, says: str | None = None) -> DriveResult:
        """Start ``run``, drive it, answer its question with ``says``, drive it to the end."""
        payload = {"agent": AGENT, "cause": "request", "definition_hash": DEFINITION,
                   "budget": BUDGET}  # fmt: skip
        self.store.append(OWNER, "U1", (NewEvent("run.started", payload, run=run),))
        self.model.script, self.model.turn = script, 0
        result = drive(self.setup, self.executor, run)
        if says is not None:
            assert result.state == "held", result
            (i,) = open_interrupts(self.view(), run)
            refused = answer(self.store, OWNER, i.id, by="p", answers={"q": says}).refused
            assert refused is None, refused
            result = drive(self.setup, self.executor, run)
        return result

    def view(self) -> LogView:
        return load_view(self.store, OWNER)

    def iv(self) -> InvalidationView:
        return invalidation_view(self.view())

    def events(self, type_: str) -> list[Event]:
        return [e for e in self.view().events if e.type == type_]

    def results(self, run: str, tool: str) -> list[Mapping[str, Any]]:
        """What ``run``'s calls of ``tool`` showed the model, read back from the steps."""
        return [
            json.loads(e.payload["value"]["view"]["text"])
            for e in self.events("step.completed")
            if e.run == run and e.payload.get("tool") == tool
        ]

    def recalls(self, run: str) -> list[Mapping[str, Any]]:
        """The data of ``run``'s recall results."""
        return [r["data"] for r in self.results(run, "recall")]

    def close(self) -> None:
        self.store.close()


def ask(n: int = 1) -> ToolCall:
    return call("ask", n, questions=[{"id": "q", "text": "Which regions should vendors be in?"}])
