"""The stop request event, built in one place (ADR-P18 D1). Pure.

``run.stop_requested`` commits with the ``stop`` command's receipt in U4, under the dedup
key ``rstop:{run}/{command_id}``, so a retried click is one request.
"""

from __future__ import annotations

from aksha.runtime.cancellation.types import STOP_REQUEST, StopScope
from aksha.runtime.events.envelope import NewEvent

REQUEST_BOUNDARY = "U4"
"""The boundary a stop request commits in (a person's resolution)."""


def stop_requested(
    run: str, *, scope: StopScope, reason: str, by: str, command_id: str
) -> NewEvent:
    """A person's request that ``run`` stop, recorded so it outlives the process."""
    payload = {"scope": scope, "reason": reason, "by": by, "command_id": command_id}
    return NewEvent(STOP_REQUEST, payload, run=run)
