"""The scribe's completion check: a note is saved. Pure."""

from __future__ import annotations

from typing import Any

from aksha.runtime.api import CheckResult


def note_saved(view: Any) -> CheckResult:
    """A ``note`` artifact is saved."""
    return CheckResult(view.latest("note") is not None, "save a note first")
