"""The kill boundary (ADR-P18 D6): ask a unit to settle, wait a bounded grace, then terminate.

This is the line around a tool body that does not stop on request. The caller records the
outcome: ``cancelled`` for a tool with no external effects, ``effects_unknown`` for an
effectful one, never success. The waits are the unit's own (``KillableUnit.wait``), so no
clock is read here.
"""

from __future__ import annotations

from aksha.runtime.cancellation.ports import KillableUnit
from aksha.runtime.cancellation.types import KillOutcome, KillPolicy


def kill_boundary(unit: KillableUnit, policy: KillPolicy) -> KillOutcome:
    """End ``unit``: ``settled`` if it stopped within the grace, else ``killed``."""
    if unit.settled():
        return "settled"
    unit.request_settle()
    if unit.wait(policy.grace_seconds):
        return "settled"
    unit.terminate()
    unit.wait(policy.reap_seconds)
    return "killed"
