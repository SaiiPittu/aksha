"""``Session``: the session API's single writer over one event store (ADR-P19 D1 to D4, D9).

Every interface reaches the runtime through one ``Session``: in server mode the runtime
thread owns it (:mod:`aksha.runtime.session.worker`), in embedded mode the CLI command
holds it for its one invocation. It does two jobs, and only on the thread that holds it:

1. :meth:`Session.dispatch` takes one named command (:mod:`.named`) and commits its
   facts with its receipt, through the existing command functions of each primitive. The
   receipt means committed, not done (D3). A command this runtime cannot carry out is
   refused ``unsupported`` with a stored receipt, and is absent from :meth:`commands`, so
   the capability record never claims it (D7).
2. :meth:`Session.step` runs one pass of one runnable run: one stop-policy evaluation and
   at most the one turn it authorises (:func:`aksha.runtime.loop.drive.drive_pass`). The
   runtime thread drains its command queue between passes, so an answer or a stop typed
   during a long run is committed within one turn (D4). :meth:`Session.run_until_yield`
   repeats passes until every open run of the owner waits on a person or has ended.

Every read is owner-scoped: an ID of another owner reads exactly like an unknown one
(D9). Nothing here keeps state a restart would lose; the executors and outbox workers it
caches per owner are rebuilt from the store.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactKind, RelationKind
from aksha.runtime.cancellation.command import stop
from aksha.runtime.cancellation.ending import honour_stop
from aksha.runtime.cancellation.requests import runs_to_resume_for_stop
from aksha.runtime.context.reentry import ReentrySetting
from aksha.runtime.context.types import ContextPolicy
from aksha.runtime.definitions.types import Agent
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.ids import LogIdSource
from aksha.runtime.execution.ports import Clock, DurableStepExecutor, IdSource
from aksha.runtime.execution.recovery import RecoveryReport, recover_on_startup, resume_owner
from aksha.runtime.grounding.ports import RecordReader
from aksha.runtime.interrupts.approvals import ApprovalTool, approval_authority
from aksha.runtime.interrupts.commands import (
    answer,
    approve,
    edit,
    finalize,
    grant_budget,
    handoff,
    reject,
)
from aksha.runtime.interrupts.reducers import approvals, interrupts
from aksha.runtime.invalidation.commands import (
    accept_review,
    apply_source_change,
    reject_review,
)
from aksha.runtime.invalidation.ports import VersionedSource
from aksha.runtime.loop.drive import drive, drive_pass
from aksha.runtime.loop.types import LoopSetup
from aksha.runtime.models.routing import AliasTable
from aksha.runtime.outbox.ports import SystemOfRecord
from aksha.runtime.outbox.types import OutboxEntry
from aksha.runtime.outbox.worker import OutboxWorker
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS, RunSummary, open_runs
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.router.commands import answer_route, submit
from aksha.runtime.router.router import Router
from aksha.runtime.router.types import RoutableAgent
from aksha.runtime.sandbox.types import Availability
from aksha.runtime.session import named as n
from aksha.runtime.session.commands import derive_command_id, dispatch
from aksha.runtime.session.start import Startable, start_spec
from aksha.runtime.session.types import (
    Command,
    CommandAccepted,
    CommandReceipt,
    CommandRefused,
    CommandSpec,
)
from aksha.runtime.session.views import SessionStatus, session_status
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import InstanceRef
from aksha.runtime.tasks.view import TaskView
from aksha.runtime.tools.registry import ToolRegistry

UNSUPPORTED = "unsupported"
"""The refusal of a command this runtime, as configured, cannot carry out (D7)."""

STOP_CANDIDATE = "resume"
"""The candidate a held run with a pending stop is ended at (recovery's, ADR-P18 D3)."""

AGENT_REQUIRED = "agent_required"
"""The refusal of a message that names no agent where no router can pick one."""

NOT_FOUND = "not_found"
"""The refusal of a command naming an item the owner does not have (D9)."""

DEFAULT_MAX_PASSES = 100_000
"""More passes than any run needs: a bound on a runtime bug, never a run limit."""


@dataclass(frozen=True)
class SessionAgent:
    """One loaded agent as a session runs it: the loop's setup minus the store and owner."""

    agent: Agent
    tree: CompiledTree
    registry: ToolRegistry
    table: AliasTable
    definition_hash: str
    instructions: str = ""
    briefs: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    kinds: Mapping[str, ArtifactKind] = field(default_factory=lambda: MappingProxyType({}))
    relations: Mapping[str, RelationKind] = field(default_factory=lambda: MappingProxyType({}))
    context_policy: ContextPolicy | None = None
    run_extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    reentry: ReentrySetting | None = None

    @property
    def name(self) -> str:
        """The agent's name."""
        return self.agent.name

    def setup(
        self,
        store: EventStore,
        owner: str,
        request: str,
        *,
        sandbox: Availability | None = None,
        blobs: BlobStore | None = None,
        reader: RecordReader | None = None,
    ) -> LoopSetup:
        """The loop's setup for one of ``owner``'s runs of this agent.

        ``reader`` is the record reader its tool bodies read versioned sources through.
        """
        limits = self.agent.limits
        extra: dict[str, Any] = {} if limits is None else {"limits": limits}
        return LoopSetup(
            store=store,
            owner=owner,
            agent=self.agent,
            tree=self.tree,
            registry=self.registry,
            table=self.table,
            instructions=self.instructions,
            request=request,
            briefs=self.briefs,
            context_policy=self.context_policy,
            artifacts=ArtifactStore(self.kinds, self.relations, blobs=blobs),
            kinds=self.kinds,
            sandbox=sandbox,
            definition_hash=self.definition_hash,
            blobs=blobs,
            reader=reader,
            **extra,
        )

    def routable(self) -> RoutableAgent:
        """The agent as the router and a request run's ``run.started`` see it (decision 36)."""
        return RoutableAgent(self.name, self.agent.purpose, self.definition_hash, self.reentry)

    def run_fields(self) -> dict[str, Any]:
        """What ``run.started`` records for this agent beyond its name and cause."""
        fields: dict[str, Any] = {**self.run_extra, **self.table.run_started_fields()}
        if self.agent.budget is not None:
            fields["budget"] = self.agent.budget.as_payload()
        return fields

    def approval_specs(self) -> dict[str, Any]:
        """Each tool that needs approval for an external effect, by name."""
        out: dict[str, Any] = {}
        for name in self.registry.names():
            spec = self.registry.get(name).spec
            if spec.approval is not None and spec.external is not None:
                out[name] = spec
        return out


class Session:
    """The single writer of the session API (module doc)."""

    def __init__(
        self,
        store: EventStore,
        clock: Clock,
        agents: Sequence[SessionAgent],
        *,
        router: Router | None = None,
        sandbox: Callable[[], Availability | None] = lambda: None,
        blobs: BlobStore | None = None,
        systems: Mapping[str, SystemOfRecord] | None = None,
        sources: Sequence[VersionedSource] = (),
        reader: RecordReader | None = None,
        ids: IdSource | None = None,
    ) -> None:
        """Serve ``agents`` over ``store`` (a writer handle); ``reader`` serves tool reads."""
        self.store = store
        self.clock = clock
        self.agents: Mapping[str, SessionAgent] = MappingProxyType({a.name: a for a in agents})
        self.router = router
        self.sandbox = sandbox
        self.blobs = blobs
        self.systems: Mapping[str, SystemOfRecord] = MappingProxyType(dict(systems or {}))
        self.sources = tuple(sources)
        self.reader = reader
        self.ids: IdSource = ids if ids is not None else LogIdSource(store)
        self._executors: dict[str, StepExecutor] = {}
        startable = {
            a.name: Startable(a.routable(), a.run_fields())
            for a in agents
        }  # fmt: skip
        self._start = start_spec(startable, self.ids, self.sandbox)

    # --- what this runtime accepts --------------------------------------------------------

    def commands(self) -> frozenset[str]:
        """The named commands :meth:`dispatch` carries out here (the capability record's)."""
        out = {"SendMessage", "AnswerInterrupt", "Approve", "Reject", "GrantBudget",
               "Finalize", "Stop", "AcceptReview", "RejectReview"}  # fmt: skip
        if any(a.approval_specs() for a in self.agents.values()):
            out |= {"EditAction", "Handoff"}
        if self.sources:
            out.add("ApplySourceChange")
        return frozenset(out)

    # --- dispatch ---------------------------------------------------------------------------

    def dispatch(self, command: n.NamedCommand) -> CommandReceipt:
        """Commit ``command`` with its receipt, or return the stored receipt of a retry."""
        if type(command).__name__ not in self.commands():
            return self._unsupported(command)
        if isinstance(command, n.SendMessage):
            return self._send(command)
        if isinstance(command, n.AnswerInterrupt):
            return self._answer(command)
        if isinstance(command, n.Approve):
            return approve(self.store, command.owner, command.approval_id,
                           command.shown_signature, by=command.by,
                           command_id=command.command_id)  # fmt: skip
        if isinstance(command, n.Reject):
            return reject(self.store, command.owner, command.approval_id, by=command.by,
                          note=command.note, command_id=command.command_id)  # fmt: skip
        if isinstance(command, n.EditAction | n.Handoff):
            return self._approval_tool_command(command)
        if isinstance(command, n.GrantBudget):
            return grant_budget(self.store, command.owner, command.interrupt_id,
                                dict(command.extra),  # type: ignore[arg-type]
                                by=command.by, command_id=command.command_id)  # fmt: skip
        if isinstance(command, n.Finalize):
            agent = self._agent_of_interrupt(command.owner, command.interrupt_id)
            if agent is None:
                return self._not_found(command, command.interrupt_id)
            return finalize(self.store, command.owner, command.interrupt_id, tree=agent.tree,
                            by=command.by, command_id=command.command_id)  # fmt: skip
        if isinstance(command, n.Stop):
            return stop(self.store, command.owner, command.run_id, reason=command.reason,
                        command_id=command.command_id, by=command.by)  # fmt: skip
        if isinstance(command, n.AcceptReview):
            # The fresh request rebuilds the moved call with the tools the outbox re-checks
            # against (ADR-P17 B1), so the approval it asks for is one execution can carry out.
            agent = self._agent_of_interrupt(command.owner, command.review_id)
            tools = None if agent is None else self._approval_tools(agent, None, None)
            return accept_review(self.store, command.owner, command.review_id,
                                 command.card_hash, self.ids, tools=tools,
                                 command_id=command.command_id)  # fmt: skip
        if isinstance(command, n.RejectReview):
            return reject_review(self.store, command.owner, command.review_id,
                                 command.card_hash, self.ids,
                                 command_id=command.command_id)  # fmt: skip
        if isinstance(command, n.ApplySourceChange):
            return apply_source_change(self.store, command.owner, self.sources,
                                       command.change_set,
                                       command_id=command.command_id)  # fmt: skip
        return self._unsupported(command)

    def _send(self, command: n.SendMessage) -> CommandReceipt:
        agent = command.agent
        if agent is None and self.router is None and len(self.agents) == 1:
            agent = next(iter(self.agents))
        if agent is None and (self.router is None or command.headless is True):
            reason = "a headless start names its agent" if command.headless is True else (
                "no router is configured; name the agent")  # fmt: skip
            return self._refuse(command, "send_message", command.session,
                                {"text": command.text}, AGENT_REQUIRED, reason)  # fmt: skip
        if agent is None:
            assert self.router is not None
            sent, _ = submit(self.store, command.owner, self.router, command.session,
                             command.text, command_id=command.command_id)  # fmt: skip
            return sent.receipt
        args: dict[str, Any] = {"text": command.text, "agent": agent}
        if command.headless is True:
            args["headless"] = True
        cmd = Command("send_message", command.owner, command.session, args, command.command_id)
        return dispatch(self.store, cmd, self._start)

    def _answer(self, command: n.AnswerInterrupt) -> CommandReceipt:
        if command.agent is not None:
            if self.router is None:
                return self._unsupported(command)
            return answer_route(self.store, command.owner, self.router, command.interrupt_id,
                                command.agent, by=command.by,
                                command_id=command.command_id)  # fmt: skip
        agent = self._agent_of_interrupt(command.owner, command.interrupt_id)
        return answer(
            self.store,
            command.owner,
            command.interrupt_id,
            by=command.by,
            text=command.text,
            answers=dict(command.answers) or None,
            tree=None if agent is None else agent.tree,
            command_id=command.command_id,
        )

    def _approval_tool_command(self, command: n.EditAction | n.Handoff) -> CommandReceipt:
        view = load_view(self.store, command.owner)
        approval = approvals(view).get(command.approval_id)
        agent = None if approval is None else self._agent_of_run(view, approval.run)
        if approval is None or agent is None:
            return self._not_found(command, command.approval_id)
        tools = self._approval_tools(agent, approval.run, approval.instance)
        if isinstance(command, n.EditAction):
            return edit(self.store, command.owner, command.approval_id, dict(command.changes),
                        tools=tools, ids=self.ids, command_id=command.command_id)  # fmt: skip
        return handoff(self.store, command.owner, command.approval_id, tools=tools,
                       definition_hash=agent.definition_hash,
                       command_id=command.command_id)  # fmt: skip

    def _approval_tools(
        self, agent: SessionAgent, run: str | None, instance: str | None
    ) -> dict[str, ApprovalTool]:
        tree = agent.tree
        ref = None if run is None or instance is None else InstanceRef.parse(run, instance)

        def view_of(log: LogView) -> Any:
            return log if ref is None else TaskView(log, tree, ref)

        return {name: ApprovalTool(spec, view_of=view_of)
                for name, spec in agent.approval_specs().items()}  # fmt: skip

    def _agent_of_run(self, view: LogView, run: str | None) -> SessionAgent | None:
        summary = None if run is None else rebuild(RUNS, view).get(run)
        return None if summary is None else self.agents.get(summary.agent)

    def _agent_of_interrupt(self, owner: str, interrupt_id: str) -> SessionAgent | None:
        view = load_view(self.store, owner)
        found = interrupts(view).get(interrupt_id)
        return None if found is None else self._agent_of_run(view, found.run)

    def _refuse(
        self,
        command: n.NamedCommand,
        name: str,
        session: str,
        args: Mapping[str, Any],
        code: str,
        message: str,
    ) -> CommandReceipt:
        def decide(_: Command, __: LogView) -> CommandAccepted | CommandRefused:
            return CommandRefused(code, message)

        cmd = Command(name, command.owner, session, dict(args), command.command_id)
        return dispatch(self.store, cmd, CommandSpec(name, "U4", decide))

    def _unsupported(self, command: n.NamedCommand) -> CommandReceipt:
        name = _snake(type(command).__name__)
        session = getattr(command, "session", None) or command.owner
        message = f"{type(command).__name__} is not available in this runtime"
        return self._refuse(command, name, session, _args(command), UNSUPPORTED, message)

    def _not_found(self, command: n.NamedCommand, ident: str) -> CommandReceipt:
        name = _snake(type(command).__name__)
        return self._refuse(command, name, command.owner, _args(command), NOT_FOUND,
                            f"no {ident}")  # fmt: skip

    # --- running ----------------------------------------------------------------------------

    def runnable(self, owner: str) -> tuple[str, ...]:
        """The owner's open runs of a loaded agent, in ID order."""
        runs = rebuild(RUNS, load_view(self.store, owner))
        return tuple(r for r in open_runs(runs) if runs[r].agent in self.agents)

    def step(self, owner: str) -> bool:
        """One pass of the first open run of ``owner`` that can move; False if none can.

        A run that waits on a person and has a pending stop gets the one ``resume``
        candidate ADR-P18 D3 gives it, so a stop sent while a run is held ends it
        ``stopped`` without waiting for an answer that will never come.
        """
        for run in self.runnable(owner):
            state = drive_pass(self._setup(owner, run), self._executor(owner), run)
            if state == "running":
                return True
            stopping = state == "held" and run in runs_to_resume_for_stop(
                load_view(self.store, owner)
            )
            if stopping and honour_stop(self.store, owner, run, STOP_CANDIDATE):
                return True
        return False

    def run_until_yield(self, owner: str, *, max_passes: int = DEFAULT_MAX_PASSES) -> int:
        """Passes until every open run of ``owner`` waits on a person or ends; their count."""
        for done in range(max_passes):
            if not self.step(owner):
                return done
        raise RuntimeError(f"{owner}'s runs did not yield within {max_passes} passes")

    def loaded(self) -> dict[str, str]:
        """Each loaded agent's definition hash, by name (what recovery compares)."""
        return {a.name: a.definition_hash for a in self.agents.values()}

    def recover(self) -> RecoveryReport:
        """Recovery on startup over every owner with an open run (ADR-P02 D10); drives nothing."""
        route = None if self.router is None else self.router.route_check
        outbox = None if not self.systems else self._worker
        return recover_on_startup(self.store, self.loaded(), outbox=outbox,
                                  route_check=route)  # fmt: skip

    def resume(self, owner: str) -> tuple[str, ...]:
        """Recover ``owner`` again and drive each in-flight run until it yields (``aksha resume``).

        Each resumed run gets its resume record first, so the restart limit counts it.
        """

        def driver(run: RunSummary, steps: DurableStepExecutor, /) -> None:
            drive(self._setup(owner, run.run), steps, run.run)

        return resume_owner(self.store, owner, driver, clock=self.clock, loaded=self.loaded(),
                            outbox=self._outbox(owner))  # fmt: skip

    def status(self, owner: str, session: str) -> SessionStatus:
        """Where ``session`` stands (``views.session_status``)."""
        return session_status(load_view(self.store, owner), session)

    def _setup(self, owner: str, run: str) -> LoopSetup:
        view = load_view(self.store, owner)
        agent = self._agent_of_run(view, run)
        assert agent is not None, f"{run} is not a run of a loaded agent"
        return agent.setup(self.store, owner, _request(view, run), sandbox=self.sandbox(),
                           blobs=self.blobs, reader=self.reader)  # fmt: skip

    def _executor(self, owner: str) -> StepExecutor:
        held = self._executors.get(owner)
        if held is None:
            held = StepExecutor(self.store, owner, self.clock, outbox=self._outbox(owner),
                                blobs=self.blobs)  # fmt: skip
            self._executors[owner] = held
        return held

    def _outbox(self, owner: str) -> OutboxWorker | None:
        return None if not self.systems else self._worker(owner)

    def _worker(self, owner: str) -> OutboxWorker:
        tools: dict[str, ApprovalTool] = {}
        hashes: dict[str, str] = {}
        for agent in self.agents.values():
            for name, spec in agent.approval_specs().items():
                tools[name] = ApprovalTool(spec)
                hashes[name] = agent.definition_hash

        def definition(entry: OutboxEntry) -> str:
            return hashes.get(entry.tool, "")

        authority = approval_authority(tools, definition)
        return OutboxWorker(self.store, owner, self.clock, self.systems, authority=authority)


def _snake(name: str) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")


def _args(command: n.NamedCommand) -> dict[str, Any]:
    skip = {"owner", "command_id", "by"}
    return {k: _plain(v) for k, v in command.__dict__.items() if k not in skip}


def _plain(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(v) for v in value]
    return value


def _request(view: LogView, run: str) -> str:
    """The text of the message that opened ``run`` (empty for a run no message opened)."""
    message: str | None = None
    for e in view.events:
        if e.type == "route.decided" and e.payload.get("run") == run:
            message = str(e.payload.get("message"))
            break
    if message is None:
        return ""
    for e in view.events:
        if e.type == "user.message" and e.payload.get("message") == message:
            return str(e.payload.get("text", ""))
    return ""


def derive_id(command: n.NamedCommand) -> str:
    """The runtime's derived command ID of a named command with none (ADR-P03 D5)."""
    name = _snake(type(command).__name__)
    session = getattr(command, "session", None) or command.owner
    return derive_command_id(Command(name, command.owner, session, _args(command)))


def receipts(store: EventStore, owner: str, ids: Iterable[str]) -> dict[str, CommandReceipt]:
    """The stored receipts of ``ids`` (the receipt index by command ID), where recorded."""
    out: dict[str, CommandReceipt] = {}
    for ident in ids:
        e = store.by_dedup_key(owner, f"cmd:{owner}/{ident}")
        if e is not None:
            accepted = e.type == "command.accepted"
            out[ident] = CommandReceipt(
                ident, e.seq, accepted, None if accepted else str(e.payload["code"]), True
            )
    return out
