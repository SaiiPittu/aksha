"""The ``stop`` command (ADR-P18 D1): it appends a stop request, and its receipt means committed.

``stop`` goes through the session API's one write path (``session.commands.dispatch``), so
the request commits with ``command.accepted`` in one transaction under the command's
idempotency key, and a retried click returns the first receipt. The client learns the run
ended from the event stream, not from the receipt. Refusals, each a ``command.refused``
receipt and no request:

1. ``scope_not_built``: ``scope="turn"`` is designed only (D7).
2. ``not_found``: the owner has no such run (another owner's run is not found, as in every
   session call).
3. ``run_not_open``: the run has already ended.
"""

from __future__ import annotations

from aksha.runtime.cancellation.facts import REQUEST_BOUNDARY, stop_requested
from aksha.runtime.cancellation.types import DEFAULT_REASON, StopScope
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS
from aksha.runtime.projections.view import LogView
from aksha.runtime.session.commands import derive_command_id, dispatch
from aksha.runtime.session.types import (
    Command,
    CommandAccepted,
    CommandReceipt,
    CommandRefused,
    CommandSpec,
)
from aksha.runtime.storage.ports import EventStore

SCOPE_NOT_BUILT = "scope_not_built"
NOT_FOUND = "not_found"
RUN_NOT_OPEN = "run_not_open"


def decide_stop(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
    """The stop command's decision, read from the owner's log. Pure."""
    a = command.args
    run, scope = str(a["run"]), a["scope"]
    if scope != "run":
        return CommandRefused(SCOPE_NOT_BUILT, f"a {scope!r} stop is designed, not built")
    summary = rebuild(RUNS, view).get(run)
    if summary is None:
        return CommandRefused(NOT_FOUND, f"no run {run}")
    if summary.outcome is not None:
        return CommandRefused(RUN_NOT_OPEN, f"{run} has already ended {summary.outcome}")
    request = stop_requested(run, scope="run", reason=str(a["reason"]), by=str(a["by"]),
                             command_id=str(command.command_id))  # fmt: skip
    return CommandAccepted((request,))


STOP = CommandSpec("stop", REQUEST_BOUNDARY, decide_stop)
"""The stop command's spec: it commits in U4, a person's resolution."""


def stop(
    store: EventStore,
    owner: str,
    run_id: str,
    scope: StopScope = "run",
    reason: str = DEFAULT_REASON,
    command_id: str | None = None,
    *,
    by: str | None = None,
    session: str | None = None,
) -> CommandReceipt:
    """Ask that ``run_id`` stop; return the receipt ("committed", not "stopped").

    ``by`` is the principal who asked (default the owner); ``session`` the session the
    command came from (default the owner's). Without a ``command_id`` one is derived, so
    the same click retried is the same command.
    """
    args = {"run": run_id, "scope": scope, "reason": reason, "by": by or owner}
    command = Command("stop", owner, session or owner, args, command_id)
    if command_id is None:
        command = Command("stop", owner, command.session, args, derive_command_id(command))
    return dispatch(store, command, STOP)
