"""Headless runs (ADR-P11 D12, decision 26, option B): never ask, every deny holds. Pure.

A run started headless carries ``headless: true`` on its ``run.started``, fixed for the
run. The gate's ``resolve_ask`` is the one place the flag changes a decision: an ask
becomes an allow; refusals are untouched. A headless run runs inside a sandbox, so a
start with no available sandbox backend is refused ``sandbox_required`` before any
``run.started`` is written (:func:`headless_start_refusal`).
"""

from __future__ import annotations

from aksha.runtime.projections.view import LogView
from aksha.runtime.sandbox.types import Availability

SANDBOX_REQUIRED = "sandbox_required"


def headless_start_refusal(headless: bool, sandbox: Availability | None) -> str | None:
    """``sandbox_required`` for a headless start with no available sandbox backend; else None."""
    if not headless:
        return None
    if sandbox is None or not sandbox.available:
        return SANDBOX_REQUIRED
    return None


def started_headless(view: LogView, run: str) -> bool:
    """Whether ``run``'s ``run.started`` fixed it headless (False when unrecorded)."""
    for e in view.events:
        if e.type == "run.started" and e.run == run:
            return e.payload.get("headless") is True
    return False
