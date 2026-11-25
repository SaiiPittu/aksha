"""``dispatch``: the one write path of the session API, with command receipts (ADR-P03 D5, D6).

1. The command ID is the client's, or derived (:func:`derive_command_id`). The decision,
   the receipt and every event the command writes see the same ID.
2. A receipt already stored under ``cmd:{owner}/{command_id}`` is returned as a
   duplicate, and the command's decision is not run again. A receipt of another command
   under the same ID is refused (``command_id_reused``).
3. Otherwise the command's decision reads the owner's log and returns its events (or a
   refusal). ``command.accepted`` and those events commit in one transaction, in the
   command's boundary; a refusal commits ``command.refused`` alone. Both carry the same
   dedup key, so a command ID has exactly one receipt.
4. When one of the command's own events collides with a recorded dedup key (a decision
   on a pending item already decided, ``decide:{approval_id}``), nothing of it commits
   and the command is refused ``already_decided`` (D6), whatever its command ID.

The receipt means "committed", not "done": work the command starts runs after
``dispatch`` returns.
"""

from __future__ import annotations

from dataclasses import replace

from aksha.runtime.canonical import sha256_hex
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.projections.view import load_view
from aksha.runtime.session.errors import CommandIdReused, CommandNameMismatch
from aksha.runtime.session.types import Command, CommandAccepted, CommandReceipt, CommandSpec
from aksha.runtime.storage.errors import AlreadyRecorded
from aksha.runtime.storage.ports import EventStore

ALREADY_DECIDED = "already_decided"
"""The refusal of a second decision on one pending item (D6)."""


def command_key(owner: str, command_id: str) -> str:
    """The dedup key of a command's receipt."""
    return f"cmd:{owner}/{command_id}"


def derive_command_id(command: Command) -> str:
    """``d-`` and 32 hex digits over the session, name, arguments, item answered and last reply.

    An up-arrow retry of the same words after the same reply is the same command; the same
    words after a newer reply are a new one.
    """
    basis = {
        "session": command.session,
        "name": command.name,
        "args": command.args,
        "answers": command.answers,
        "after_seq": command.after_seq,
    }
    return "d-" + sha256_hex(basis)[:32]


def dispatch(store: EventStore, command: Command, spec: CommandSpec) -> CommandReceipt:
    """Commit ``command``'s receipt and events, or return the receipt already stored."""
    if spec.name != command.name:
        raise CommandNameMismatch(message=f"{command.name!r} dispatched as {spec.name!r}")
    command_id = command.command_id or derive_command_id(command)
    command = replace(command, command_id=command_id)
    args_hash = "sha256:" + sha256_hex(command.args)
    key = command_key(command.owner, command_id)
    stored = store.by_dedup_key(command.owner, key)
    if stored is not None:
        return _duplicate(stored, command, args_hash)
    decision = spec.decide(command, load_view(store, command.owner))
    if isinstance(decision, CommandAccepted):
        accepted = _receipt_event("command.accepted", command, command_id, args_hash)
        try:
            events = store.append(
                command.owner, spec.boundary, (accepted, *decision.events), decision.artifacts
            )
        except AlreadyRecorded as e:
            if e.existing.dedup_key == key:
                return _duplicate(e.existing, command, args_hash)
            return _refuse(store, command, spec, command_id, args_hash, ALREADY_DECIDED,
                           f"{e.existing.dedup_key} is already recorded")  # fmt: skip
        return _receipt(events[0], duplicate=False)
    return _refuse(store, command, spec, command_id, args_hash, decision.code, decision.message)


def _refuse(
    store: EventStore,
    command: Command,
    spec: CommandSpec,
    command_id: str,
    args_hash: str,
    code: str,
    message: str,
) -> CommandReceipt:
    refused = _receipt_event("command.refused", command, command_id, args_hash, code, message)
    try:
        (event,) = store.append(command.owner, spec.boundary, (refused,))
    except AlreadyRecorded as e:
        return _duplicate(e.existing, command, args_hash)
    return _receipt(event, duplicate=False)


def _receipt_event(
    type_: str,
    command: Command,
    command_id: str,
    args_hash: str,
    code: str | None = None,
    message: str = "",
) -> NewEvent:
    payload: dict[str, str] = {"command_id": command_id, "name": command.name,
                               "args_hash": args_hash}  # fmt: skip
    if command.answers is not None:
        payload["answers"] = command.answers
    if code is not None:
        payload["code"] = code
        if message:
            payload["message"] = message
    return NewEvent(type_, payload, session=command.session)


def _duplicate(stored: Event, command: Command, args_hash: str) -> CommandReceipt:
    p = stored.payload
    if (p.get("name"), p.get("args_hash")) != (command.name, args_hash):
        raise CommandIdReused(
            message=f"{p.get('command_id')} already names a {p.get('name')} command "
            "with other arguments"
        )
    return _receipt(stored, duplicate=True)


def _receipt(event: Event, *, duplicate: bool) -> CommandReceipt:
    p = event.payload
    accepted = event.type == "command.accepted"
    return CommandReceipt(
        command_id=str(p["command_id"]),
        seq=event.seq,
        accepted=accepted,
        refused=None if accepted else str(p["code"]),
        duplicate=duplicate,
    )
