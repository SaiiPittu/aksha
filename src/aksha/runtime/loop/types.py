"""Agent loop values (ADR-P06): turn ends, call positions, the loop's setup and a run's state.

1. ``TurnEndKind`` and ``TurnEnd``: how one turn of one Task instance ended, the stop
   candidate stop-policy evaluation judges (:meth:`TurnEnd.candidate`).
2. ``Signal`` and ``PositionOutcome``: what one call position of a turn recorded, and
   whether it ends the turn's run of calls (a finish, a question, an approval request, a
   spent budget), so every later position is recorded ``skipped``.
3. ``LoopSetup``: what the loop is given once per agent and owner. Everything else it
   reads is a projection of the event store, rebuilt for every turn (P-2).
4. ``DriveResult``: where :func:`~aksha.runtime.loop.drive.drive` left a run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, get_args

from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.artifacts.types import ArtifactKind
from aksha.runtime.budgets.types import Limits
from aksha.runtime.context.types import AssemblyInputs, ContextPolicy, effective_policy
from aksha.runtime.definitions.types import Agent
from aksha.runtime.grounding.ports import RecordReader
from aksha.runtime.models.routing import AliasTable
from aksha.runtime.policy.permissions import ceiling_of
from aksha.runtime.policy.types import Permissions, SourceVersion
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.sandbox.types import Availability
from aksha.runtime.stop_policy.types import AUTOMATIC_CAUSE, CandidateKind, StopCandidate
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import InstanceRef, Task
from aksha.runtime.tools.registry import ToolRegistry

TurnEndKind = Literal[
    "tool_results",  # the model called tools and every position is recorded
    "final_answer",  # text with no tool calls, or the reserved ``finish`` tool
    "interrupt",  # a call asked a person a question or a choice (P17)
    "approval",  # a call requested an approval for an exact action (P17)
    "budget",  # a call was refused with budget_exhausted (P14)
    "malformed",  # the reply was cut off, refused, empty or unreadable
    "context_overflow",  # over the context limit before sending, or the provider refused it
    "fault",  # model unavailable after retries, a step mismatch on resume, a stop
]
TURN_END_KINDS: frozenset[str] = frozenset(get_args(TurnEndKind))

CANDIDATE_OF: Mapping[str, CandidateKind] = MappingProxyType(
    {
        "tool_results": "tools",
        "final_answer": "finish",
        "interrupt": "interrupted",
        "approval": "approval",
        "budget": "budget",
        "malformed": "malformed",
        "context_overflow": "overflow",
        "fault": "fault",
    }
)
"""The stop candidate each turn-end kind is (ADR-P16 names the candidate kinds)."""

STEP_MISMATCH = "step_mismatch"
"""The reason of a turn whose recorded step was reached with other arguments: a run fault."""

RUN_FAULTS: frozenset[str] = frozenset({STEP_MISMATCH})
"""Fault reasons that end the run, not only the instance: the run's code changed under it."""

Signal = Literal["finish", "interrupt", "approval", "budget"]
"""What a call position did that ends the turn's run of calls (ADR-P06 D5)."""

SIGNALS: frozenset[str] = frozenset(get_args(Signal))

SIGNAL_FIELD = "signal"
"""The field of a tool step's recorded value that names its signal."""


def skip_code(signal: Signal) -> str:
    """The code a position after ``signal`` is recorded ``skipped`` with: ``after_approval``."""
    return f"after_{signal}"


@dataclass(frozen=True)
class PositionOutcome:
    """One call position as recorded: its step outcome and code, and its signal if any."""

    position: int
    tool: str
    outcome: str
    code: str | None = None
    signal: Signal | None = None


@dataclass(frozen=True)
class TurnEnd:
    """How turn ``turn`` of Task instance ``instance`` of ``run_id`` ended.

    ``reason`` is set for the kinds that need one (``context_limit``, ``context_overflow``,
    ``model_unavailable``, ``step_mismatch``, ``run_stopped``). ``last_seq`` is the sequence
    number of the last event the turn wrote (0 when it wrote none).
    """

    run_id: str
    instance: str
    turn: int
    kind: TurnEndKind
    reason: str | None = None
    last_seq: int = 0

    def __post_init__(self) -> None:
        """Refuse a kind outside the eight."""
        if self.kind not in TURN_END_KINDS:
            raise ValueError(f"turn-end kind {self.kind!r} is not one of the eight")

    def candidate(self, cause: str = AUTOMATIC_CAUSE) -> StopCandidate:
        """The stop candidate this turn end is. A step mismatch is the run's fault."""
        ref = InstanceRef.parse(self.run_id, self.instance)
        kind = CANDIDATE_OF[self.kind]
        if self.kind == "fault" and self.reason in RUN_FAULTS:
            return StopCandidate(self.run_id, kind, None, self.turn, cause, self.reason or "")
        return StopCandidate(self.run_id, kind, ref, self.turn, cause, self.reason or "")


@dataclass(frozen=True)
class LoopSetup:
    """What the agent loop is given for one owner's runs of one agent (ADR-P06 D2).

    The values here are the loaded definition and the adapters; nothing in it changes
    during a run, and nothing a run writes lives here. ``request`` is the person's request
    text and ``instructions`` the agent's instruction text, both read by the loader;
    ``briefs`` holds each Task's brief text by Task path. ``ceiling`` defaults to the
    agent's whole tool list. ``artifacts`` is the save path of store tools; without it a
    body's save is refused ``store_unavailable``. ``reader`` is the record reader a tool body
    reads versioned sources through (ADR-P13 D1); without it a body's span drafts are refused.
    """

    store: EventStore
    owner: str
    agent: Agent
    tree: CompiledTree
    registry: ToolRegistry
    table: AliasTable
    instructions: str = ""
    request: str = ""
    briefs: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    ceiling: Permissions | None = None
    context_policy: ContextPolicy | None = None
    artifacts: ArtifactStore | None = None
    kinds: Mapping[str, ArtifactKind] = field(default_factory=lambda: MappingProxyType({}))
    sources: Mapping[str, SourceVersion] = field(default_factory=lambda: MappingProxyType({}))
    sandbox: Availability | None = None
    definition_hash: str | None = None
    limits: Limits = field(default_factory=Limits)
    blobs: BlobStore | None = None
    reader: RecordReader | None = None

    def view(self) -> LogView:
        """The owner's log as it stands now."""
        return load_view(self.store, self.owner)

    def task(self, ref: InstanceRef) -> Task:
        """The declared Task of ``ref``."""
        return self.tree.node(ref.task_path).task

    def permissions(self) -> Permissions:
        """The run ceiling: the declared one, or the agent's whole list."""
        return self.ceiling if self.ceiling is not None else ceiling_of(self.agent)

    def policy(self) -> ContextPolicy:
        """The context policy a run uses (the agent's own, or the default with masking)."""
        if self.context_policy is not None:
            return self.context_policy
        return effective_policy(self.agent.context_policy)

    def inputs(self, ref: InstanceRef) -> AssemblyInputs:
        """The definition's text ``ref``'s context starts from."""
        brief = self.briefs.get(ref.task_path, "")
        return AssemblyInputs(self.instructions, brief, self.request)


DriveState = Literal["ended", "held"]
"""``ended``: the run has its ``run.ended``. ``held``: it waits on a person."""


@dataclass(frozen=True)
class DriveResult:
    """Where a drive left a run: ended with an outcome, or held for a person."""

    run: str
    state: DriveState
    outcome: str | None = None
    turns: tuple[TurnEnd, ...] = ()
