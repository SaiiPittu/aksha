"""Stop values: the opt-ins an agent declares, and the candidates, verdicts and decisions.

ADR-P16. What an agent declares (D4): ``StopPolicyName``, ``BUILTIN_STOP_POLICIES``,
``StopPolicyOptIn`` and its five constructors ``Goal``, ``Plan``, ``ForcedTool``,
``TodoReminder`` and ``Recite``. The set is closed: an agent opts into a built-in stop policy with
parameters and can never add a stop-policy class (decision 10).

What evaluation works with:

1. ``StopCandidate``: a point where control returns to the runtime (eleven kinds).
2. ``StopView``: everything a stop policy may read: the log, the compiled Task tree, the
   run, the limits, the budget and the meter evaluation reads first. No store, no clock,
   no model.
3. ``Verdict``: one policy's answer (seven kinds), with the facts it implies.
4. ``Prep``: what the stack adds to the next model request (brief lines, tool narrowing).
5. ``StopDecision``: one evaluation's result, the ``stop.decided`` record and every fact
   that commits with it.
6. ``Hold``, ``RunScope`` and ``AgentScope``: what the hold predicate answers (D6, S1).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, get_args

from aksha.runtime.budgets.types import Budget, Limits

if TYPE_CHECKING:
    from aksha.runtime.artifacts.ports import BlobStore
    from aksha.runtime.budgets.meter import Meter
    from aksha.runtime.context.types import ContextPolicy
    from aksha.runtime.events.envelope import NewEvent
    from aksha.runtime.projections.view import LogView
    from aksha.runtime.tasks.tree import CompiledTree
    from aksha.runtime.tasks.types import Check, InstanceRef

StopPolicyName = Literal["goal", "plan", "forced_tool", "todo_reminder", "recite"]

BUILTIN_STOP_POLICIES: frozenset[str] = frozenset(
    {"goal", "plan", "forced_tool", "todo_reminder", "recite"}
)
"""The only stop policies an agent may opt into."""


@dataclass(frozen=True)
class StopPolicyOptIn:
    """An opt-in to one built-in stop policy, with its parameters (read-only)."""

    name: StopPolicyName
    params: Mapping[str, object]

    def __post_init__(self) -> None:
        """Freeze ``params`` so a declared opt-in cannot change after load."""
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))


def Goal(check: Check) -> StopPolicyOptIn:
    """Veto a finish while ``check`` fails."""
    return StopPolicyOptIn("goal", {"check": check})


def Plan(kind: str, accept: Check) -> StopPolicyOptIn:
    """Require an accepted ``kind`` plan artifact before work starts."""
    return StopPolicyOptIn("plan", {"kind": kind, "accept": accept})


def ForcedTool(tool: str, when: Check) -> StopPolicyOptIn:
    """Force the model to call ``tool`` while ``when`` holds."""
    return StopPolicyOptIn("forced_tool", {"tool": tool, "when": when})


def TodoReminder(every: int) -> StopPolicyOptIn:
    """List unmet child checks in the context every ``every`` turns; never vetoes."""
    return StopPolicyOptIn("todo_reminder", {"every": every})


RECITE_MAX_CHARS = 12_000
"""The default cap on one recitation: what a supervisor's work board may cost per turn."""


Render = Callable[..., "Sequence[str] | str"]
"""A recitation's render: a pure function of a ``TaskView`` to the lines to show."""


def Recite(render: Render, max_chars: int = RECITE_MAX_CHARS) -> StopPolicyOptIn:
    """Recite ``render``'s lines, a pure projection over the Task view, on every turn.

    ``render`` takes the instance's ``TaskView`` and returns the lines to show (a tuple of
    strings, or one string). The
    runtime caps the text at ``max_chars`` and places it as data, never as instructions,
    after the request. A supervisor reads its work board this way (ADR-P16 S4).
    """
    return StopPolicyOptIn("recite", {"render": render, "max_chars": max_chars})


# --- candidates -----------------------------------------------------------------------

CandidateKind = Literal[
    "enter",  # the run just pushed this instance
    "next",  # an instance ended or parked; the run decides what runs next
    "tools",  # a turn ran tool calls and asked for nothing else
    "finish",  # the model asked to finish (``finish``, or a text-only reply)
    "malformed",  # the reply was cut off or could not be read
    "interrupted",  # a call opened an interrupt (a question or a choice)
    "approval",  # a call became a pending approval
    "budget",  # the gate refused a call mid-turn because a budget was spent
    "fault",  # a step mismatch, a changed definition, a permanent model error
    "resume",  # recovery found a turn end a crash swallowed
    "overflow",  # the context did not fit after compaction
]
CANDIDATE_KINDS: frozenset[str] = frozenset(get_args(CandidateKind))

AUTOMATIC_CAUSE = "continuation"
"""The cause of an evaluation the runtime starts on its own (an automatic start)."""


@dataclass(frozen=True)
class StopCandidate:
    """One point where control returns to the runtime.

    ``instance`` is None for a run-level candidate (``next``, a run ``fault``). ``cause`` is
    ``continuation`` (automatic), ``resume`` (recovery) or ``command:<name>`` when a
    person's command led here; only an automatic cause is held by human state. ``reason``
    says why for a ``fault``. The sequence number and the meter are read by evaluation,
    not carried here, so a candidate cannot disagree with the log.
    """

    run: str
    kind: CandidateKind
    instance: InstanceRef | None = None
    after_turn: int | None = None
    cause: str = AUTOMATIC_CAUSE
    reason: str = ""

    def __post_init__(self) -> None:
        """Refuse a kind outside the closed set."""
        if self.kind not in CANDIDATE_KINDS:
            raise ValueError(f"stop candidate kind {self.kind!r} is not one of the eleven")

    def offered(self, kind: CandidateKind, instance: InstanceRef | None) -> StopCandidate:
        """This candidate as offered to another level: a pushed child, or the parent."""
        return replace(self, kind=kind, instance=instance)


DEFAULT_LIMITS = Limits()
"""The runtime's default limits, for a view built without its own."""


# --- the view -------------------------------------------------------------------------


@dataclass(frozen=True)
class StopView:
    """All a stop policy may read (D1): data only, no store, clock, model or randomness.

    ``budget`` is the run's budget when the caller narrows it (else the one recorded on
    ``run.started``); ``agent_policies`` are the agent-wide opt-ins, applied to every leaf
    Task outside its own. ``meter`` is filled by evaluation's first step.
    ``context_policy`` is the agent's context policy, which the re-entry choice reads
    (decision 36); None is the default policy. ``blobs`` is the blob store the choice
    reads offloaded step payloads through (a port, not data: it is read, never written).
    """

    log: LogView
    tree: CompiledTree
    run: str
    limits: Limits = DEFAULT_LIMITS
    budget: Budget | None = None
    agent_policies: tuple[StopPolicyOptIn, ...] = ()
    meter: Meter | None = None
    context_policy: ContextPolicy | None = None
    blobs: BlobStore | None = None


# --- verdicts and preparation ----------------------------------------------------------

VerdictKind = Literal["pass", "continue", "push", "hold", "done", "fail", "stop"]
VERDICT_KINDS: frozenset[str] = frozenset(get_args(VerdictKind))

Outcome = Literal["complete", "partial", "blocked", "failed", "stopped"]
"""How a run ends; ``stopped`` is a person's stop (ADR-P18)."""


@dataclass(frozen=True)
class InterruptRequest:
    """An interrupt a stop decision raises itself: the budget interrupt (ADR-P14, D3).

    ``limit`` is the spent dimension, with its use and allowance.
    """

    interrupt: str
    limit: str
    used: int
    allowed: int


@dataclass(frozen=True)
class Verdict:
    """One stop policy's answer to one candidate.

    ``reason``: on CONTINUE the model reads it, on HOLD and FAIL a person does. ``child`` is
    the instance a PUSH offers the candidate to; ``outcome`` is a STOP's; ``raises`` is the
    budget interrupt a HOLD opens; ``facts`` are what the verdict implies (an entry, a
    completion check evaluation), committed with the decision; ``resumes_turn`` marks a
    CONTINUE that re-authorises a turn a crash swallowed, which charges nothing. ``code``
    names a limit that produced the verdict (``turn_limit``, ``stop_veto_limit``, a budget
    dimension) or a failure (``check_raised``); ``counts`` are the counts behind a limit.
    """

    kind: VerdictKind
    reason: str = ""
    child: InstanceRef | None = None
    outcome: Outcome | None = None
    raises: InterruptRequest | None = None
    facts: tuple[NewEvent, ...] = ()
    resumes_turn: bool = False
    code: str = ""
    counts: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        """Refuse a kind outside the seven, a PUSH without a child, a STOP without outcome."""
        if self.kind not in VERDICT_KINDS:
            raise ValueError(f"verdict {self.kind!r} is not one of the seven")
        if self.kind == "push" and self.child is None:
            raise ValueError("a PUSH names the child it offers the candidate to")
        if self.kind == "stop" and self.outcome is None:
            raise ValueError("a STOP names the run's outcome")


PASS = Verdict("pass")
"""No opinion: offer the candidate to the next policy out."""


@dataclass(frozen=True)
class Prep:
    """What the stack adds to the next model request, composed outer to inner.

    ``tools`` None means no narrowing; otherwise the tools kept, intersected, never widened
    (``finish`` and ``ask`` always stay: the gate keeps reserved tools). ``narrowed_by``
    names the innermost policy that narrowed. ``tool_choice`` is a hint a live model
    adapter may pass on (forced tool choice); the fake model ignores it. ``data`` are
    recitations (``Recite``): text the runtime derived from the log, placed after the
    request inside a data fence, never among the instructions.
    """

    brief: tuple[str, ...] = ()
    tools: frozenset[str] | None = None
    narrowed_by: str | None = None
    tool_choice: str | None = None
    data: tuple[str, ...] = ()


# --- the decision ---------------------------------------------------------------------

PolicyVerdict = tuple[str, VerdictKind]
"""A trail entry: the policy's ID and its verdict."""


@dataclass(frozen=True)
class StopDecision:
    """One evaluation's result: the ``stop.decided`` record and the facts it implies.

    ``instance`` is the instance the final verdict concerns (a CONTINUE's or a HOLD's), or
    None for the run. ``pops`` are the instances popped on the way, in order. ``facts``
    end with the record itself and commit in one transaction (U3).
    """

    seq_no: int
    candidate: StopCandidate
    instance: InstanceRef | None
    stack: tuple[str, ...]
    trail: tuple[PolicyVerdict, ...]
    decided_by: str
    verdict: Verdict
    seq_read: int
    replaced: PolicyVerdict | None = None
    limit: str | None = None
    counts: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    pops: tuple[tuple[str, Literal["done", "fail"]], ...] = ()
    turn: int | None = None
    facts: tuple[NewEvent, ...] = ()

    @property
    def record(self) -> NewEvent:
        """The ``stop.decided`` record, the last fact."""
        return self.facts[-1]


# --- the hold predicate ---------------------------------------------------------------

HoldReason = Literal["stop_requested", "interrupt_open", "stopped_by_person"]
"""Why human state holds a scope (D6); the set is closed."""


@dataclass(frozen=True)
class RunScope:
    """A run, held by its stop request or a run-scoped interrupt (the budget interrupt)."""

    run: str


@dataclass(frozen=True)
class SessionScope:
    """A session's intake, held by an open ``route`` question (ADR-P17 S5, ADR-P23)."""

    session: str


@dataclass(frozen=True)
class AgentScope:
    """An (owner, agent) pair; the owner is the view's. Held only by a person's stop."""

    agent: str


@dataclass(frozen=True)
class Hold:
    """Why a scope is held: the reason, the seq of the event that holds it, the scope."""

    reason: HoldReason
    by: int
    scope: str
