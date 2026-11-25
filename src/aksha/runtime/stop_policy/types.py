"""Stop values: the stop-policy opt-in an agent declares, and later candidates and decisions.

Built so far (ADR-P16 D4): ``StopPolicyName``, ``BUILTIN_STOP_POLICIES``,
``StopPolicyOptIn`` and its four constructors ``Goal``, ``Plan``, ``ForcedTool`` and
``TodoReminder``. ``StopCandidate``, ``Verdict``, ``Prep`` and ``StopDecision`` arrive with
stop-policy evaluation.

The set is closed: an agent opts into a built-in stop policy with parameters and can
never add a stop-policy class (decision 10).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from aksha.runtime.tasks.types import Check

StopPolicyName = Literal["goal", "plan", "forced_tool", "todo_reminder"]

BUILTIN_STOP_POLICIES: frozenset[str] = frozenset({"goal", "plan", "forced_tool", "todo_reminder"})
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
