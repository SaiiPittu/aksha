"""Pure completion checks for the conformance agent. Each reads only the view it is given."""

from __future__ import annotations

from typing import Any

from aksha.runtime.api import CheckResult


def note_read(view: Any) -> CheckResult:
    """The note for this key was read."""
    return CheckResult(ok=bool(getattr(view, "read", False)), message="read the note first")


def line_saved(view: Any) -> CheckResult:
    """A line was saved for this key."""
    return CheckResult(ok=bool(getattr(view, "saved", False)), message="save one line")


def summary_saved(view: Any) -> CheckResult:
    """The summary was saved."""
    return CheckResult(ok=bool(getattr(view, "summary", False)), message="save the summary")


def every_line_in_summary(view: Any) -> CheckResult:
    """The summary covers every saved line."""
    missing = tuple(getattr(view, "missing", ()))
    return CheckResult(ok=not missing, message="cover every line", count=len(missing))
