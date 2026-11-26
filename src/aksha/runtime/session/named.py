"""The named commands: one closed union of frozen values, each a request to change state.

ADR-P19 D1: every state change an interface makes is one of these values, passed to
``Session.dispatch``. Each carries the tenant (``owner``) first, a client-generated
``command_id`` (D2; random per invocation and reused only for that invocation's retries,
never derived from the arguments by a client) and ``by``, the role of whoever sent it.
A command with no ``command_id`` gets the runtime's derived one (ADR-P03 D5), so a test or
an embedded caller that sends none still gets an idempotent receipt.

Amendments from the build (ADR-P19, 26 Nov 2026): ``OpenSession`` is not in the union,
because a session is the ID its first message names and no ``session.opened`` fact
exists; ``SendMessage`` carries ``headless`` (ADR-P11 D12: fixed at the run's start);
``AcceptReview`` and ``RejectReview`` carry the shown ``card_hash`` (ADR-P05), not edits.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

DEFAULT_BY = "person"
"""The role a command is recorded under when its sender names none."""


@dataclass(frozen=True, kw_only=True)
class CommandBase:
    """What every command carries: the tenant, its command ID and the sender's role."""

    owner: str
    command_id: str | None = None
    by: str = DEFAULT_BY


@dataclass(frozen=True, kw_only=True)
class SendMessage(CommandBase):
    """A person's message to a session; naming ``agent`` starts that agent's run."""

    session: str
    text: str
    agent: str | None = None
    headless: bool = False


@dataclass(frozen=True, kw_only=True)
class AnswerInterrupt(CommandBase):
    """An answer to an input, choice or route interrupt (``agent`` for a route only)."""

    interrupt_id: str
    text: str | None = None
    answers: Mapping[str, str] = field(default_factory=dict)
    agent: str | None = None


@dataclass(frozen=True, kw_only=True)
class Approve(CommandBase):
    """Grant an approval; ``shown_signature`` is the signature the person was shown."""

    approval_id: str
    shown_signature: str


@dataclass(frozen=True, kw_only=True)
class Reject(CommandBase):
    """Refuse an approval."""

    approval_id: str
    note: str | None = None


@dataclass(frozen=True, kw_only=True)
class EditAction(CommandBase):
    """Change a pending action's arguments; voids the approval and asks again."""

    approval_id: str
    changes: Mapping[str, object]


@dataclass(frozen=True, kw_only=True)
class Handoff(CommandBase):
    """Hand a granted action to a handoff run."""

    approval_id: str


@dataclass(frozen=True, kw_only=True)
class AcceptReview(CommandBase):
    """Accept a diff review; ``card_hash`` is the card the person was shown."""

    review_id: str
    card_hash: str


@dataclass(frozen=True, kw_only=True)
class RejectReview(CommandBase):
    """Reject a diff review."""

    review_id: str
    card_hash: str


@dataclass(frozen=True, kw_only=True)
class GrantBudget(CommandBase):
    """Answer a budget interrupt with extra units per dimension."""

    interrupt_id: str
    extra: Mapping[str, int]


@dataclass(frozen=True, kw_only=True)
class ApplySourceChange(CommandBase):
    """Apply one change set over the configured sources (S1)."""

    change_set: str


@dataclass(frozen=True, kw_only=True)
class Stop(CommandBase):
    """Ask a run to stop (ADR-P18)."""

    run_id: str
    reason: str = "Stopped by user"


@dataclass(frozen=True, kw_only=True)
class Finalize(CommandBase):
    """Answer a budget interrupt with partial finalization (S4, ADR-P24)."""

    interrupt_id: str


@dataclass(frozen=True, kw_only=True)
class Steer(CommandBase):
    """Input for a running run in a lane (S2, ADR-P27). Reserved: no lane is built."""

    run_id: str
    text: str
    lane: Literal["steer", "follow_up", "amend"] = "steer"


NamedCommand = (
    SendMessage
    | AnswerInterrupt
    | Approve
    | Reject
    | EditAction
    | Handoff
    | AcceptReview
    | RejectReview
    | GrantBudget
    | ApplySourceChange
    | Stop
    | Finalize
    | Steer
)
"""The closed union (D1)."""

COMMAND_NAMES: tuple[str, ...] = (
    "SendMessage",
    "AnswerInterrupt",
    "Approve",
    "Reject",
    "EditAction",
    "Handoff",
    "AcceptReview",
    "RejectReview",
    "GrantBudget",
    "ApplySourceChange",
    "Stop",
    "Finalize",
    "Steer",
)
"""Every member's name, in the union's order; the capability record lists a subset."""
