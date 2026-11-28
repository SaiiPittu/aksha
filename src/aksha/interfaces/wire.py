"""The wire form of commands, receipts and statuses, shared by the HTTP server and the CLI.

A command travels as ``{"command": "<union member>", "command_id": ..., "by": ...,
"args": {...}}``; the owner comes from the ``X-Aksha-User`` header (the web twin of
``--user``), never from the body. :func:`command_from_json` refuses an unknown member, a
missing argument or an argument the member does not have with :class:`WireInvalid`
(``validation``, HTTP 422, exit code 2), so a typo never reaches the runtime.

Exit codes, shared by every CLI command (the HTTP status is beside each):

====  =========================================================  ====
Code  Meaning                                                    HTTP
====  =========================================================  ====
0     done: the run ended complete, or the command needed no run  200
1     internal error                                             500
2     usage or validation error                                  422
3     waiting for a person's answer (input, choice, route, budget,
      review)                                                    200
4     waiting for an approval                                    200
5     the run ended partial                                      200
6     the run ended stopped                                      200
7     refused: the command was refused, or the ID is unknown     409, 404
8     environment: the writer lock is held, the store or an agent
      failed to load                                             423, 503
9     the run ended blocked or failed                            200
10    the hash chain does not verify                             200
====  =========================================================  ====
"""

from __future__ import annotations

import dataclasses
import secrets
from collections.abc import Mapping
from typing import Any

from aksha.runtime.errors import AkshaError
from aksha.runtime.session import named
from aksha.runtime.session.types import CommandReceipt
from aksha.runtime.session.views import RunStatus, SessionStatus, Waiting

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_WAITING_INPUT = 3
EXIT_WAITING_APPROVAL = 4
EXIT_PARTIAL = 5
EXIT_STOPPED = 6
EXIT_REFUSED = 7
EXIT_ENVIRONMENT = 8
EXIT_FAILED = 9
EXIT_CHAIN_BROKEN = 10

OUTCOME_EXIT = {
    "complete": EXIT_OK,
    "partial": EXIT_PARTIAL,
    "stopped": EXIT_STOPPED,
    "blocked": EXIT_FAILED,
    "failed": EXIT_FAILED,
}

NOT_FOUND_CODES = frozenset({"not_found", "approval_not_found"})
"""Refusals that mean "no such item for this owner": HTTP 404 rather than 409 (D9)."""

COMMANDS: Mapping[str, type[named.CommandBase]] = {
    name: getattr(named, name) for name in named.COMMAND_NAMES
}


class WireInvalid(AkshaError):
    """A request body that is not a valid command."""

    default_code = "validation"


def new_command_id() -> str:
    """A random command ID for one invocation (ADR-P19 D2): never derived from arguments."""
    return "c-" + secrets.token_hex(12)


def new_session_id() -> str:
    """A random session ID for a conversation a client starts."""
    return "S-" + secrets.token_hex(6)


def command_from_json(owner: str, body: Mapping[str, Any]) -> named.NamedCommand:
    """The named command ``body`` describes, for ``owner`` (module doc)."""
    name = body.get("command")
    cls = COMMANDS.get(str(name))
    if cls is None:
        raise WireInvalid(message=f"unknown command {name!r}; one of {sorted(COMMANDS)}")
    args = body.get("args") or {}
    if not isinstance(args, Mapping):
        raise WireInvalid(message="args is an object")
    fields = {f.name for f in dataclasses.fields(cls)} - {"owner", "command_id", "by"}
    extra = set(args) - fields
    if extra:
        raise WireInvalid(message=f"{name} has no argument {sorted(extra)}")
    values: dict[str, Any] = {k: args[k] for k in args}
    types = {f.name: str(f.type) for f in dataclasses.fields(cls)}
    for key, value in values.items():
        expected = _WIRE_TYPES.get(types[key])
        if expected is not None and not isinstance(value, expected):
            raise WireInvalid(message=f"{name}.{key} is {types[key]}, not {type(value).__name__}")
    command_id = body.get("command_id")
    by = body.get("by") or named.DEFAULT_BY
    try:
        command = cls(owner=owner, command_id=None if command_id is None else str(command_id),
                      by=str(by), **values)  # fmt: skip
    except TypeError as e:
        raise WireInvalid(message=f"{name}: {e}") from None
    return command  # type: ignore[return-value]


_WIRE_TYPES: Mapping[str, tuple[type, ...]] = {
    "bool": (bool,),
    "str": (str,),
    "str | None": (str, type(None)),
    "Mapping[str, str]": (Mapping,),
    "Mapping[str, int]": (Mapping,),
    "Mapping[str, object]": (Mapping,),
}
"""The JSON types each annotation of a named command accepts; ``"false"`` is no bool."""


def command_to_json(command: named.NamedCommand) -> dict[str, Any]:
    """The wire form of ``command`` (the owner travels in the header)."""
    args = {
        f.name: _plain(getattr(command, f.name))
        for f in dataclasses.fields(command)
        if f.name not in ("owner", "command_id", "by")
    }
    return {"command": type(command).__name__, "command_id": command.command_id,
            "by": command.by, "args": args}  # fmt: skip


def _plain(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(v) for v in value]
    return value


def receipt_to_json(receipt: CommandReceipt) -> dict[str, Any]:
    """The wire form of a receipt."""
    return {
        "command_id": receipt.command_id,
        "seq": receipt.seq,
        "status": "accepted" if receipt.accepted else "refused",
        "code": receipt.refused,
        "replayed": receipt.duplicate,
    }


def receipt_from_json(data: Mapping[str, Any]) -> CommandReceipt:
    """A receipt back from its wire form."""
    accepted = data.get("status") == "accepted"
    return CommandReceipt(str(data["command_id"]), int(data["seq"]), accepted,
                          None if accepted else str(data.get("code")),
                          bool(data.get("replayed")))  # fmt: skip


def waiting_to_json(w: Waiting) -> dict[str, Any]:
    """The wire form of one open interrupt."""
    return dataclasses.asdict(w)


def waiting_from_json(data: Mapping[str, Any]) -> Waiting:
    """One open interrupt back from its wire form (JSON lists back to tuples)."""
    pairs = ("questions", "facts")
    fields = {k: v for k, v in data.items() if k in {f.name for f in dataclasses.fields(Waiting)}}
    for name in pairs:
        fields[name] = tuple((str(a), str(b)) for a, b in fields.get(name) or ())
    fields["options"] = tuple(str(o) for o in fields.get("options") or ())
    fields["option_labels"] = tuple(str(o) for o in fields.get("option_labels") or ())
    return Waiting(**fields)


def run_to_json(run: RunStatus) -> dict[str, Any]:
    """The wire form of a run's status."""
    out = dataclasses.asdict(run)
    out["waiting_on"] = [waiting_to_json(w) for w in run.waiting_on]
    return out


def run_from_json(data: Mapping[str, Any]) -> RunStatus:
    """A run's status back from its wire form."""
    waiting = tuple(waiting_from_json(w) for w in data.get("waiting_on") or ())
    return RunStatus(str(data["run"]), str(data["agent"]), data.get("session"),
                     data["state"], data.get("outcome"), bool(data.get("headless")),
                     waiting)  # fmt: skip


def session_to_json(status: SessionStatus) -> dict[str, Any]:
    """The wire form of a session's status."""
    return {
        "session": status.session,
        "run": None if status.run is None else run_to_json(status.run),
        "waiting_on": [waiting_to_json(w) for w in status.waiting_on],
        "seq": status.seq,
        "yielded": status.yielded,
    }


def run_exit_code(run: RunStatus | None, waiting: tuple[Waiting, ...] = ()) -> int:
    """The exit code of where a run stands (module doc)."""
    pending = waiting or (() if run is None else run.waiting_on)
    if run is not None and run.state == "ended":
        return OUTCOME_EXIT.get(run.outcome or "", EXIT_FAILED)
    if any(w.kind == "approval" for w in pending):
        return EXIT_WAITING_APPROVAL
    if pending:
        return EXIT_WAITING_INPUT
    return EXIT_OK


def refusal_status(code: str | None) -> int:
    """The HTTP status of a refused receipt: 404 for an unknown item, else 409."""
    return 404 if code in NOT_FOUND_CODES else 409
