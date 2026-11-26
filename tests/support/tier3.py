"""A bench for tier 3 in the agent loop: one small agent whose tools fetch and run programs.

The agent ``runner`` has one Task, ``work``, and three tools: ``fetch_page`` (a read tool
under the egress policy ``web.docs``), ``peek`` (a read tool with no egress policy that
tries the network anyway) and ``run_script`` (an exec tool that runs ``python3 -c`` in a
box and returns its output). The network is a fake resolver and transport; the sandbox
backend is whatever the test gives (the recording fake, or the real bubblewrap backend).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from aksha.adapters.blobs.filesystem import FilesystemBlobStore
from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactKind
from aksha.runtime.definitions.types import Agent
from aksha.runtime.egress.ports import HttpRequest, HttpResponse, Network
from aksha.runtime.egress.types import EgressPolicy
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.loop.drive import drive
from aksha.runtime.loop.types import DriveResult, LoopSetup
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.projections.view import load_view
from aksha.runtime.sandbox.ports import Sandbox
from aksha.runtime.sandbox.tier import ExecutionTier
from aksha.runtime.sandbox.types import SandboxNeeds
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import CheckResult, Task
from aksha.runtime.tasks.view import TaskView
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import (
    Err,
    Ok,
    RetryPolicy,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
    ToolUse,
)
from tests.support.events import START
from tests.support.loop import Recorder

OWNER = "o"
RUN = "R-0001"
AGENT = "runner"
DEFINITION = "sha256:" + "c" * 64
NOTE = ArtifactKind("note", {"type": "object"})
DOCS = EgressPolicy("web.docs", allow_hosts=("docs.example.org", "rebind.example.org"))
URL = {
    "type": "object",
    "properties": {"url": {"type": "string"}},
    "required": ["url"],
    "additionalProperties": False,
}
CODE = {
    "type": "object",
    "properties": {"code": {"type": "string"}},
    "required": ["code"],
    "additionalProperties": False,
}


class FakeResolver:
    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        return {"docs.example.org": ("93.184.215.14",), "rebind.example.org": ("127.0.0.1",)
                }.get(host, ())  # fmt: skip


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(
        self, request: HttpRequest, address: str, *, timeout_s: float, max_bytes: int
    ) -> HttpResponse:
        self.sent.append((request.url, address))
        if request.url.endswith("/moved"):
            return HttpResponse(302, {"location": "https://rebind.example.org/meta"}, b"",
                                request.url)  # fmt: skip
        return HttpResponse(200, {}, b"SYN page text", request.url)


def fetch_page(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    got = ctx.http.get(str(args["url"]), timeout_s=5)
    return ToolOutcome(Ok({"status": got.status, "text": got.body.decode()}))


def run_script(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    assert ctx.exec is not None
    got = ctx.exec.run(["python3", "-c", str(args["code"])])
    if not got.ok:
        return ToolOutcome(Err(got.code or "exec_failed", got.stderr[:500]))
    return ToolOutcome(Ok({"stdout": got.stdout, "outputs": sorted(got.outputs)}))


def has_note(view: TaskView) -> CheckResult:
    return CheckResult(view.latest("note") is not None, "save a note first")


def agent() -> Agent:
    specs = (
        ToolDecl(
            ToolSpec(
                "fetch_page",
                AGENT,
                "read",
                "Fetch a page.",
                URL,
                egress="web.docs",
                retry=RetryPolicy(max_retries=2, retry_on=frozenset({"egress_refused"})),
            ),
            fetch_page,
        ),
        ToolDecl(ToolSpec("peek", AGENT, "read", "Peek at a URL.", URL), fetch_page),
        ToolDecl(
            ToolSpec(
                "run_script",
                AGENT,
                "exec",
                "Run Python.",
                CODE,
                sandbox=SandboxNeeds(),
                retry=RetryPolicy(timeout_ms=60_000),
            ),
            run_script,
        ),
    )
    return Agent(
        name=AGENT,
        purpose="Fetch pages and run scripts.",
        instructions="prompts/instructions.md",
        reads=(),
        artifacts=(NOTE,),
        tools=(*specs, ToolUse("save_artifact")),
        tasks=(
            Task(
                "work",
                tools=("fetch_page", "peek", "run_script", "save_artifact"),
                writes=("note",),
                done=has_note,
            ),
        ),
    )


class TierBench:
    """One owner's SQLite store, the ``runner`` agent, and tier 3 as the test sets it."""

    def __init__(self, root: Path, model: Recorder, *, box: Sandbox | None = None,
                 network: bool = True, session: str | None = None) -> None:  # fmt: skip
        self.clock = FakeClock(START)
        self.model = model
        self.agent = agent()
        self.store = open_store(root / "aksha.sqlite", self.clock, writer=True)
        self.blobs = FilesystemBlobStore(root / "blobs")
        self.transport = FakeTransport()
        probes = () if box is None else (box.probe(),)
        self.tier = ExecutionTier(
            box=box, probes=probes,
            network=Network(FakeResolver(), self.transport) if network else None,
            egress={"web.docs": DOCS},
        )  # fmt: skip
        listed = {t.spec.name: t for t in self.agent.tools if isinstance(t, ToolDecl)}
        self.setup = LoopSetup(
            store=self.store, owner=OWNER, agent=self.agent,
            tree=compile_tree(AGENT, self.agent.tasks),
            registry=ToolRegistry(self.agent, listed, ()),
            table=bind_aliases((), "fake", {"generate": model}),
            instructions="You fetch and run.", request="Do the work.",
            artifacts=ArtifactStore({f"{AGENT}.note": NOTE}, blobs=self.blobs),
            definition_hash=DEFINITION, blobs=self.blobs,
            sandbox=self.tier.availability, tier=self.tier,
        )  # fmt: skip
        self.executor = StepExecutor(self.store, OWNER, self.clock, blobs=self.blobs)
        self.start(RUN, session)

    def start(self, run: str, session: str | None = None) -> None:
        """Record ``run.started`` for ``run``, in ``session`` when one is given."""
        self.store.append(OWNER, "U1", (NewEvent("run.started", {
            "agent": AGENT, "cause": "request", "definition_hash": DEFINITION,
            "budget": {"model_turns": 100, "tool_calls": 100, "active_ms": 10**9},
        }, run=run, session=session),))  # fmt: skip

    def with_tier(self, **changes: Any) -> None:
        self.tier = replace(self.tier, **changes)
        self.setup = replace(self.setup, tier=self.tier, sandbox=self.tier.availability)

    def drive(self, run: str = RUN) -> DriveResult:
        return drive(self.setup, self.executor, run)

    def tool_steps(self, tool: str) -> list[Event]:
        return [e for e in load_view(self.store, OWNER).events
                if e.type == "step.completed" and e.payload.get("tool") == tool]  # fmt: skip

    def close(self) -> None:
        self.store.close()
