"""A bench for the agent loop tests (ADR-P06): one small coding agent on a SQLite store.

The agent ``coder`` has one Task, ``edit``: it reads files, runs the tests, may ask a
person, may send a document (an external effect that needs approval) and is done once it
saved a ``note``. Every tool body is a spy, so a test can say which bodies ran. The model
is any generation adapter: scripted replies, or a rule that answers by turn number.

``Bench.restart`` closes the store and opens it again with a fresh executor, as a new
process after a kill would.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactKind
from aksha.runtime.cancellation.command import stop
from aksha.runtime.definitions.types import Agent
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.recovery import resume_owner
from aksha.runtime.interrupts.types import ApprovalRule, DeclaredInterrupt
from aksha.runtime.loop.drive import LoopDriver, drive
from aksha.runtime.loop.types import DriveResult, LoopSetup
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.rules import RuleGenerationModel
from aksha.runtime.models.types import GenerationRequest, GenerationResult, ToolCall
from aksha.runtime.outbox.types import EffectSpec
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import CheckResult, InstanceRef, Task
from aksha.runtime.tasks.view import TaskView
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import Ok, ToolContext, ToolDecl, ToolOutcome, ToolSpec, ToolUse
from tests.support.events import START

OWNER = "o"
RUN = "R-0001"
AGENT = "coder"
DEFINITION = "sha256:" + "a" * 64
EDIT = InstanceRef(RUN, "edit")
NOTE = ArtifactKind("note", {"type": "object"})
BUDGET = {"model_turns": 1_000, "tool_calls": 1_000, "active_ms": 10**9}

PATH = {
    "type": "object",
    "properties": {"path": {"type": "string", "minLength": 1}},
    "required": ["path"],
    "additionalProperties": False,
}
SEND = {
    "type": "object",
    "properties": {"document": {"type": "string"}, "recipient_id": {"type": "string"}},
    "required": ["document", "recipient_id"],
    "additionalProperties": False,
}


class Crash(BaseException):
    """The process dies here: nothing in the runtime catches a ``BaseException``."""


def has_note(view: TaskView) -> CheckResult:
    """The completion check of ``edit``: a note is saved."""
    return CheckResult(view.latest("note") is not None, "save a note first")


@dataclass
class Bodies:
    """Spy tool bodies: ``calls`` lists each body run; ``files`` is what ``read_file`` reads.

    ``crash_on`` names a ``(tool, path)`` whose first call dies (``Crash``).
    """

    files: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    crash_on: tuple[str, str] | None = None

    def read_file(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        path = str(args["path"])
        self.calls.append(f"read_file:{path}")
        if self.crash_on == ("read_file", path):
            self.crash_on = None
            raise Crash(f"killed inside read_file {path}")
        return ToolOutcome(Ok({"path": path, "text": self.files.get(path, "")}))

    def run_tests(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        self.calls.append("run_tests")
        return ToolOutcome(Ok({"passed": True}))

    def send(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        self.calls.append("send")
        return ToolOutcome(Ok({"sent": True}))


def _send_facts(view: object, args: Mapping[str, object]) -> Mapping[str, str]:
    return {"to": str(args["recipient_id"])}


def agent(bodies: Bodies, *, turn_limit: int = 250) -> Agent:
    """The ``coder`` agent with spy bodies."""
    specs = (
        ToolSpec("read_file", AGENT, "read", "Read a file.", PATH),
        ToolSpec("run_tests", AGENT, "read", "Run the tests.", {"type": "object"}),
        ToolSpec(
            "send",
            AGENT,
            "external",
            "Send a document.",
            SEND,
            external=EffectSpec("mail", ("/document", "/recipient_id"), "reconcile"),
            approval=ApprovalRule(facts=_send_facts),
        ),
    )
    bodies_by = {"read_file": bodies.read_file, "run_tests": bodies.run_tests, "send": bodies.send}
    return Agent(
        name=AGENT,
        purpose="Edit code until the tests pass.",
        instructions="prompts/instructions.md",
        reads=(),
        artifacts=(NOTE,),
        tools=(*(ToolDecl(s, bodies_by[s.name]) for s in specs), ToolUse("save_artifact")),
        tasks=(
            Task(
                "edit",
                tools=("read_file", "run_tests", "send", "save_artifact"),
                writes=("note",),
                done=has_note,
                interrupt=DeclaredInterrupt("input", "question"),
                turn_limit=turn_limit,
            ),
        ),
    )


def call(name: str, n: int = 1, **args: Any) -> ToolCall:
    """One tool call of a reply."""
    return ToolCall(f"call-{name}-{n}", name, args)


def tools_reply(*calls: ToolCall) -> GenerationResult:
    """A reply that asks for ``calls``."""
    return GenerationResult(None, calls, "tool_calls")


SAVE_NOTE = call("save_artifact", kind="note", content={"done": True})
FINISH = call("finish", summary="done")


def finish_reply() -> GenerationResult:
    """Save the note and finish, in one reply."""
    return tools_reply(SAVE_NOTE, FINISH)


class Recorder(RuleGenerationModel):
    """A generation model that answers from a list (or a rule) and keeps every request.

    ``replies`` are given in order; an exception in the list is raised instead (a
    ``Crash`` kills the process there). ``rule`` answers when the list is empty.
    """

    def __init__(
        self,
        replies: Iterable[GenerationResult | BaseException] = (),
        rule: Callable[[GenerationRequest], GenerationResult | BaseException] | None = None,
    ) -> None:
        self.replies = list(replies)
        self.rule = rule
        self.requests: list[GenerationRequest] = []

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        reply: GenerationResult | BaseException
        if self.replies:
            reply = self.replies.pop(0)
        elif self.rule is not None:
            reply = self.rule(request)
        else:
            raise AssertionError(f"no reply left for {request.step_id}")
        if isinstance(reply, BaseException):
            raise reply
        return reply

    @property
    def steps(self) -> list[str]:
        """The step IDs the model was asked for, in order."""
        return [r.step_id for r in self.requests]


class Bench:
    """One owner's SQLite store, the ``coder`` agent and the loop's setup over them."""

    def __init__(
        self,
        path: Path,
        model: Recorder,
        bodies: Bodies | None = None,
        *,
        budget: Mapping[str, int] | None = None,
        turn_limit: int = 250,
        instructions: str = "You edit code.",
    ) -> None:
        self.path = path
        self.clock = FakeClock(START)
        self.model = model
        self.bodies = bodies or Bodies()
        self.agent = agent(self.bodies, turn_limit=turn_limit)
        self.store: EventStore = open_store(path, self.clock, writer=True)
        self.budget = dict(BUDGET if budget is None else budget)
        self.instructions = instructions
        self.setup = self._setup(self.store)
        self.executor = StepExecutor(self.store, OWNER, self.clock)

    def _setup(self, store: EventStore) -> LoopSetup:
        listed = {t.spec.name: t for t in self.agent.tools if isinstance(t, ToolDecl)}
        return LoopSetup(
            store=store,
            owner=OWNER,
            agent=self.agent,
            tree=compile_tree(AGENT, self.agent.tasks),
            registry=ToolRegistry(self.agent, listed, ()),
            table=bind_aliases((), "fake", {"generate": self.model}),
            instructions=self.instructions,
            request="Fix bug 7.",
            artifacts=ArtifactStore({f"{AGENT}.note": NOTE}),
            definition_hash=DEFINITION,
        )

    def start(self, *, headless: bool = False) -> None:
        payload: dict[str, Any] = {
            "agent": AGENT, "cause": "request", "definition_hash": DEFINITION,
            "budget": self.budget,
        }  # fmt: skip
        if headless:
            payload["headless"] = True
        self.store.append(OWNER, "U1", (NewEvent("run.started", payload, run=RUN),))

    def drive(self) -> DriveResult:
        return drive(self.setup, self.executor, RUN)

    def restart(self, **changes: Any) -> None:
        """A new process: the store opened again, a fresh executor, ``changes`` to the setup."""
        self.store.close()
        self.store = open_store(self.path, self.clock, writer=True)
        self.setup = replace(self._setup(self.store), **changes)
        self.executor = StepExecutor(self.store, OWNER, self.clock)

    def resume(self) -> tuple[str, ...]:
        """Recovery's lazy resume of the owner's in-flight runs, through the loop's driver."""
        return resume_owner(
            self.store, OWNER, LoopDriver(self.setup), clock=self.clock,
            loaded={AGENT: DEFINITION},
        )  # fmt: skip

    def view(self) -> LogView:
        return load_view(self.store, OWNER)

    def events(self, type_: str | None = None) -> list[Event]:
        return [e for e in self.view().events if type_ is None or e.type == type_]

    def completed(self, step: str) -> Event:
        found = self.store.by_dedup_key(OWNER, f"sdone:{step}")
        assert found is not None, f"{step} has no step.completed"
        return found

    def decisions(self) -> list[Event]:
        return self.events("stop.decided")

    def ended(self) -> Mapping[str, Any]:
        (end,) = self.events("run.ended")
        return end.payload

    def stop_after(self, step: str) -> None:
        """Ask the run to stop right after ``step``'s result commits (a person's click)."""
        inner = self.store
        owner = OWNER

        class Stopping:
            def __getattr__(self, name: str) -> Any:
                return getattr(inner, name)

            def append(self, o: str, boundary: str, events: Any, artifacts: Any = ()) -> Any:
                out = inner.append(o, boundary, events, artifacts)
                if any(e.type == "step.completed" and e.step == step for e in events):
                    stop(inner, owner, RUN)
                return out

        wrapped: Any = Stopping()
        self.setup = replace(self.setup, store=wrapped)
        self.executor = StepExecutor(wrapped, OWNER, self.clock)

    def close(self) -> None:
        self.store.close()
