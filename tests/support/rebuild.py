"""A bench for the rebuild tests through the real agent loop (ADR-P05 D7, D8 with ADR-P06).

The agent ``demo`` has four Tasks under neutral names: ``scope`` lists two keys, then per
key ``extract`` reads a source record through a read tool (``Ok.reads``) and saves an
extraction, ``compare`` reads the extraction through its context and saves a comparison,
and ``bundle`` reads the comparison and saves a reviewable bundle a person approves for
sending. The model is a rule over the step ID: turn 1 does the instance's work and
finishes in one reply, so every content a Task saves is a function of the record version
it read (``World``), and a source change reaches every version downstream of it.

``Rig.drive`` drives one run through ``loop.drive.drive``; ``Rig.rebuild`` opens and
drives the rebuild runs through ``loop.drive.drive_rebuilds``. Nothing here builds a
fact the runtime should build: saves, reviews, revocations and re-entries all come from
the loop, the scheduler and stop-policy evaluation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactKind, InputRef
from aksha.runtime.canonical import canonical_json
from aksha.runtime.definitions.types import Agent
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.ids import LogIdSource
from aksha.runtime.execution.types import StepId
from aksha.runtime.interrupts.approvals import approval_signature, request_events
from aksha.runtime.interrupts.types import ActionBody
from aksha.runtime.loop.drive import drive, drive_rebuilds
from aksha.runtime.loop.types import DriveResult, LoopSetup
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.types import GenerationRequest, GenerationResult
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import Task, artifact_keys
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import Ok, ToolContext, ToolDecl, ToolOutcome, ToolSpec, ToolUse
from tests.support.events import START
from tests.support.invalidation import FakeSource
from tests.support.loop import Recorder, call, tools_reply
from tests.support.tasks import has, has_own

OWNER = "o1"
AGENT = "demo"
DEFINITION = "sha256:" + "d" * 64
OBJECT: Mapping[str, Any] = {"type": "object"}
KEYS = ("C1", "C2")

KINDS: Mapping[str, ArtifactKind] = {
    f"{AGENT}.scope": ArtifactKind("scope", OBJECT),
    f"{AGENT}.extract": ArtifactKind("extract", OBJECT, keyed=True),
    f"{AGENT}.compare": ArtifactKind("compare", OBJECT, keyed=True),
    f"{AGENT}.bundle": ArtifactKind("bundle", OBJECT, keyed=True, reviewable=True),
}

KEY_ARGS = {
    "type": "object",
    "properties": {"key": {"type": "string", "minLength": 1}},
    "required": ["key"],
    "additionalProperties": False,
}

SCOPE = artifact_keys("scope", "ids")


@dataclass
class World:
    """The source records' versions per key, as the read tool and the model see them."""

    versions: dict[str, str] = field(default_factory=lambda: {k: "1" for k in KEYS})
    reads: list[str] = field(default_factory=list)

    def read_record(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        key = str(args["key"])
        self.reads.append(key)
        version = self.versions[key]
        record = InputRef("record", f"refs/K-{key}", version)
        return ToolOutcome(Ok({"key": key, "version": version}, reads=(record,)))


def tasks() -> tuple[Task, ...]:
    """``scope``, then per key ``extract``, ``compare`` (needs extract), ``bundle``."""
    save = ("save_artifact",)
    return (
        Task("scope", tools=save, writes=("scope",), done=has("scope")),
        Task(
            "extract",
            per=SCOPE,
            tools=("read_record", *save),
            writes=("extract",),
            done=has_own("extract"),
        ),
        Task(
            "compare",
            per=SCOPE,
            needs=("extract",),
            reads=("extract",),
            tools=save,
            writes=("compare",),
            done=has_own("compare"),
        ),
        Task(
            "bundle",
            per=SCOPE,
            needs=("compare",),
            reads=("compare",),
            tools=save,
            writes=("bundle",),
            done=has_own("bundle"),
        ),
    )


def agent(world: World) -> Agent:
    read = ToolSpec("read_record", AGENT, "read", "Read a source record by key.", KEY_ARGS)
    return Agent(
        name=AGENT,
        purpose="Build one bundle per key from its source record.",
        instructions="prompts/instructions.md",
        reads=(),
        artifacts=tuple(KINDS.values()),
        tools=(ToolDecl(read, world.read_record), ToolUse("save_artifact")),
        tasks=tasks(),
    )


def replies(
    world: World,
    script: dict[str, int],
    on_first: dict[str, Callable[[], object]],
    finish_only: set[str] | None = None,
) -> Any:
    """The model: each instance does its work in turn 1 and finishes; ``script`` counts calls.

    ``on_first`` runs a callback the first time an instance is asked (a change mid-run);
    a callback that returns ``"finish_only"`` makes that reply a bare ``finish``. An
    instance in ``finish_only`` always replies with a bare ``finish`` (it never saves).
    """

    def rule(request: GenerationRequest) -> GenerationResult:
        step = StepId.parse(request.step_id)
        instance = str(step.instance)
        script[instance] = script.get(instance, 0) + 1
        hook = on_first.pop(instance, None)
        if instance in (finish_only or ()) or (hook is not None and hook() == "finish_only"):
            return tools_reply(call("finish", summary="nothing to redo"))
        task, _, rest = instance.partition("[")
        key = rest.rstrip("]") or None
        finish = call("finish", summary="done")
        if task == "scope":
            save = call("save_artifact", kind="scope", content={"ids": list(KEYS)})
            return tools_reply(save, finish)
        assert key is not None
        content = {"key": key, "from": world.versions[key]}
        save = call("save_artifact", kind=task, key=key, content=content)
        if task == "extract":
            return tools_reply(call("read_record", key=key), save, finish)
        return tools_reply(save, finish)

    return rule


class Rig:
    """One owner's SQLite store, the ``demo`` agent and the loop's setup over them."""

    def __init__(self, path: Path) -> None:
        self.clock = FakeClock(START)
        self.world = World()
        self.source = FakeSource()
        self.calls: dict[str, int] = {}
        self.on_first: dict[str, Callable[[], object]] = {}
        self.finish_only: set[str] = set()
        self.model = Recorder(rule=replies(self.world, self.calls, self.on_first, self.finish_only))
        self.store: EventStore = open_store(path, self.clock, writer=True)
        a = agent(self.world)
        listed = {t.spec.name: t for t in a.tools if isinstance(t, ToolDecl)}
        self.setup = LoopSetup(
            store=self.store,
            owner=OWNER,
            agent=a,
            tree=compile_tree(AGENT, a.tasks),
            registry=ToolRegistry(a, listed, ()),
            table=bind_aliases((), "fake", {"generate": self.model}),
            instructions="Build bundles.",
            request="Build the bundles.",
            artifacts=ArtifactStore(KINDS),
            kinds=KINDS,
            definition_hash=DEFINITION,
        )
        self.executor = StepExecutor(self.store, OWNER, self.clock)
        self.ids = LogIdSource(self.store)

    def close(self) -> None:
        self.store.close()

    def change(self, key: str, version: str) -> None:
        """Key ``key``'s source record moves to ``version``; the command records it."""
        from aksha.runtime.invalidation.commands import apply_source_change

        self.source.change(OWNER, {f"refs/K-{key}": version})
        self.world.versions[key] = version
        receipt = apply_source_change(self.store, OWNER, [self.source], f"{key}-{version}")
        assert receipt.accepted

    def start(self, run: str = "R-0001", **extra: Any) -> str:
        payload = {"agent": AGENT, "cause": "request", "definition_hash": DEFINITION, **extra}
        self.store.append(OWNER, "U1", (NewEvent("run.started", payload, run=run),))
        return run

    def drive(self, run: str = "R-0001") -> DriveResult:
        return drive(self.setup, self.executor, run)

    def rebuild(self) -> tuple[DriveResult, ...]:
        return drive_rebuilds(self.setup, self.executor, self.ids)

    def view(self) -> LogView:
        return load_view(self.store, OWNER)

    def events(self, type_: str | None = None, run: str | None = None) -> list[Event]:
        return [
            e
            for e in self.view().events
            if (type_ is None or e.type == type_) and (run is None or e.run == run)
        ]

    def approve(self, approval: str, target: str, run: str = "R-0001") -> None:
        """A person's request and grant of a send of ``target`` (ADR-P17's facts, U4)."""
        from aksha.runtime.artifacts.types import ArtifactRef
        from aksha.runtime.artifacts.versions import version

        found = version(self.view(), ArtifactRef.parse(target))
        assert found is not None, target
        body = ActionBody(
            tool="send", owner=OWNER, canonical_args=canonical_json({"bundle": target}),
            targets=((target, found.content_hash),), idempotency_key=f"send:{approval}",
            definition_hash=DEFINITION, facts={},
        )  # fmt: skip
        self.store.append(OWNER, "U4", request_events(approval, body, run=run, instance=None))
        granted = {"approval": approval, "signature": approval_signature(body), "by": "person",
                   "targets": [target]}  # fmt: skip
        self.store.append(OWNER, "U4", (NewEvent("approval.granted", granted, run=run),))
