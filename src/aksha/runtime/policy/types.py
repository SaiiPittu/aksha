"""Policy gate values: rules, permissions, the gate input and its two answers (ADR-P11).

``ArgumentRule`` is a declared condition on one argument of one tool, using one matcher
from a closed set. ``Permissions`` is a tool set plus rules; the run ceiling is one.
``StopPolicyNarrowing``, ``MeterReading`` and ``SourceVersion`` are the projections the
gate reads. ``GateInput`` bundles them with the call, the agent definition and the Task;
it holds nothing the model can write except the call itself (D4).

``check`` answers ``Allow`` (with a route) or ``Refuse`` (with a code, the layer that
refused, a message the model reads and whether it counts against the tool-call budget).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, get_args

from aksha.runtime.definitions.types import Agent
from aksha.runtime.policy.errors import RuleInvalid
from aksha.runtime.policy.paths import root_problem
from aksha.runtime.sandbox.types import Availability
from aksha.runtime.tasks.types import Task
from aksha.runtime.tools.types import ToolCall

Matcher = Literal["path_under", "path_not_under", "enum", "id_in_source", "max_length"]
"""The closed set of argument matchers (D7). No lambdas, so the matrix can be printed."""

MATCHERS: frozenset[str] = frozenset(get_args(Matcher))

PATH_MATCHERS: frozenset[str] = frozenset({"path_under", "path_not_under"})
"""The matchers whose ``value`` is a workspace path root."""

RuleRole = Literal["argument", "recipient"]
"""Where a rule is checked: ``argument`` at layer 6, ``recipient`` at layer 7 (the route check
of a tool that needs approval, refused as ``recipient_not_listed``)."""

Route = Literal["dispatch", "outbox", "approval", "model", "reserved"]
"""What the durable step executor does with an allowed call."""

ROUTES: frozenset[str] = frozenset(get_args(Route))

RefusalCode = Literal[
    "budget_exhausted",
    "unknown_tool",
    "not_allowed",
    "not_in_task",
    "narrowed",
    "schema",
    "argument_rule",
    "recipient_not_listed",
    "sandbox_unavailable",
    "gate_error",
]
"""Every reason the gate refuses, one per layer plus ``gate_error`` (fail closed)."""

REFUSAL_CODES: frozenset[str] = frozenset(get_args(RefusalCode))

LAYERS: Mapping[int, str] = MappingProxyType(
    {
        0: "budget",
        1: "registered",
        2: "agent list and run ceiling",
        3: "Task list",
        4: "stop-policy narrowing",
        5: "schema",
        6: "argument rules",
        7: "route checks",
    }
)
"""The fixed layer order (D3). The first layer that refuses wins."""


@dataclass(frozen=True)
class ArgumentRule:
    """A condition on the argument at ``path`` (a JSON pointer) of ``tool``.

    ``value`` is the matcher's parameter: a path root (``path_under``, ``path_not_under``),
    the allowed values (``enum``), a source name (``id_in_source``) or a length
    (``max_length``). ``reason`` is shown to the model on refusal. ``role`` says which layer
    checks it: every rule is an ``argument`` rule (layer 6) unless it is declared as the
    ``recipient`` check (layer 7), which must use ``id_in_source``.
    """

    tool: str
    path: str
    matcher: Matcher
    value: str | tuple[str, ...] | int
    reason: str
    role: RuleRole = "argument"

    @property
    def label(self) -> str:
        """The rule as one line for a refusal's detail: ``send/to enum ('a',)``."""
        return f"{self.tool}{self.path} {self.matcher} {self.value!r}"


@dataclass(frozen=True)
class Permissions:
    """A tool set and the argument rules on it. The run ceiling is one; narrowing only removes."""

    tools: frozenset[str]
    rules: tuple[ArgumentRule, ...] = ()

    def __post_init__(self) -> None:
        """Refuse a rule that cannot be checked as written (:class:`RuleInvalid`).

        A path root must already be in normal form (``.secret``, not ``./.secret`` or ``.secret/``),
        relative and inside the workspace: ``rule_root_invalid``. A recipient rule must use
        ``id_in_source``: ``rule_role_invalid``. This is where a rule set is loaded, so a
        broken rule stops the run before any call is checked.
        """
        for rule in self.rules:
            if rule.matcher in PATH_MATCHERS:
                problem = root_problem(rule.value)
                if problem is not None:
                    raise RuleInvalid("rule_root_invalid", f"{rule.label}: {problem}")
            if rule.role == "recipient" and rule.matcher != "id_in_source":
                message = f"{rule.label}: a recipient rule uses id_in_source"
                raise RuleInvalid("rule_role_invalid", message)

    def allows(self, name: str) -> bool:
        """Whether ``name`` is in the tool set."""
        return name in self.tools

    def rules_for(self, name: str) -> tuple[ArgumentRule, ...]:
        """The rules on tool ``name``, in declaration order."""
        return tuple(r for r in self.rules if r.tool == name)


@dataclass(frozen=True)
class StopPolicyNarrowing:
    """The tools the active stop policy keeps this turn (ADR-P16 ``Prep.tools``).

    ``tools`` None means no narrowing; ``by`` names the stop policy that narrowed.
    """

    tools: frozenset[str] | None = None
    by: str | None = None

    def keeps(self, name: str) -> bool:
        """Whether ``name`` survives the narrowing (reserved tools are the gate's concern)."""
        return self.tools is None or name in self.tools


@dataclass(frozen=True)
class MeterReading:
    """The part of budget accounting (ADR-P14 ``Meter``) the gate reads: spent dimensions."""

    exhausted: frozenset[str] = frozenset()

    @property
    def spent(self) -> bool:
        """Whether any budget dimension is spent."""
        return bool(self.exhausted)


@dataclass(frozen=True)
class SourceVersion:
    """One named source at its current version: the record IDs an ``id_in_source`` rule accepts."""

    name: str
    version: str
    ids: frozenset[str]

    def holds(self, record_id: str) -> bool:
        """Whether ``record_id`` is a record of this source version."""
        return record_id in self.ids


@dataclass(frozen=True)
class GateInput:
    """Everything ``check`` reads. Only ``call`` comes from the model (D4).

    ``ceiling`` and ``headless`` are fixed on ``run.started``; ``narrowing``, ``meter`` and
    ``sources`` are projections rebuilt from the event store; ``sandbox`` is the chosen
    backend's probe.
    """

    call: ToolCall
    agent: Agent
    task: Task
    ceiling: Permissions
    narrowing: StopPolicyNarrowing
    meter: MeterReading
    sources: Mapping[str, SourceVersion]
    sandbox: Availability | None
    headless: bool = False
    """Fixed on ``run.started`` (D12, decision 26); read only by ``gate.resolve_ask``."""


class _NoTruth:
    """Refuses ``bool()``: a gate answer is narrowed with ``isinstance`` or ``match``, never truth.

    ``if check(...):`` would run the body on a refusal, because a dataclass instance is
    truthy; raising here makes that mistake fail loudly the first time it runs.
    """

    def __bool__(self) -> bool:
        """Raise ``TypeError``; test ``isinstance(decision, Allow)`` instead."""
        raise TypeError("a gate answer has no truth value; test isinstance(decision, Allow)")


@dataclass(frozen=True)
class Allow(_NoTruth):
    """The call may run along ``route``. ``counted``: whether it uses the tool-call budget."""

    route: Route
    counted: bool = True


@dataclass(frozen=True)
class Refuse(_NoTruth):
    """The call may not run. ``detail`` names the failing paths, the rule or the stop policy."""

    code: RefusalCode
    layer: int
    message: str
    counted: bool
    detail: tuple[str, ...] = ()
