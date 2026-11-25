"""Command values (ADR-P03 D5): the command, its spec, its two answers and its receipt."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.projections.view import LogView
from aksha.runtime.storage.rows import ArtifactRow


@dataclass(frozen=True)
class Command:
    """A request through the session API that changes state.

    ``command_id`` is the client's name for it; a client that sends none gets one derived
    from the session, the name, the arguments, the pending item it ``answers`` and
    ``after_seq``, the sequence number of the last reply the client saw.
    """

    name: str
    owner: str
    session: str
    args: Mapping[str, Any]
    command_id: str | None = None
    answers: str | None = None
    after_seq: int | None = None


@dataclass(frozen=True)
class CommandAccepted:
    """A command's events, committed with its ``command.accepted`` in one transaction."""

    events: Sequence[NewEvent] = ()
    artifacts: Sequence[ArtifactRow] = ()


@dataclass(frozen=True)
class CommandRefused:
    """A command the runtime refuses: ``command.refused`` with ``code`` is its receipt."""

    code: str
    message: str = ""


Decide = Callable[[Command, LogView], CommandAccepted | CommandRefused]
"""What a command does, read from the owner's log: its events, or a refusal. Pure."""


@dataclass(frozen=True)
class CommandSpec:
    """One kind of command: its name, the boundary it commits in, and its decision."""

    name: str
    boundary: str
    decide: Decide


@dataclass(frozen=True)
class CommandReceipt:
    """The stored record that a command with this ID was accepted or refused.

    The acknowledgement means "committed", not "done": what the command starts (a turn,
    an outbox drain) runs after the receipt is returned. ``duplicate`` is True when this
    is a retry answered from the store.
    """

    command_id: str
    seq: int
    accepted: bool
    refused: str | None
    duplicate: bool
