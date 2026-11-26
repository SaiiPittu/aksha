"""Refusals of stop-policy evaluation (ADR-P16), each with a stable code.

``StopRefused`` codes: ``run_not_open`` (a candidate of a run that never started or has
ended: nothing may decide its stop again) and ``evaluation_loop`` (an evaluation offered
the candidate more times than the stack can need, which only a runtime bug can cause).
"""

from __future__ import annotations

from aksha.runtime.errors import AkshaError


class StopRefused(AkshaError):
    """Stop-policy evaluation cannot answer this candidate."""

    default_code = "stop_refused"
