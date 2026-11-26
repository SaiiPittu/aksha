"""Running a completion check safely: a check that raises is a FAIL, never a crash (D2).

A completion check is the agent's pure function over a ``TaskView``. The runtime calls it
only through :func:`run_check`, which turns an exception into a failing ``CheckResult``
whose message names the exception, and a value that is not a ``CheckResult`` into one
too, so an agent bug blocks one instance instead of ending the process.
"""

from __future__ import annotations

from typing import Any

from aksha.runtime.tasks.types import Check, CheckResult

CHECK_RAISED = "check_raised"


def check_name(check: object) -> str:
    """A stable name for a check in facts: its qualified name, without the module path."""
    return str(getattr(check, "__qualname__", None) or getattr(check, "__name__", "check"))


def run_check(check: Check, view: Any) -> tuple[CheckResult, bool]:
    """``(result, raised)``: the check's result, or a failing one when it raised."""
    try:
        result: object = check(view)
    except Exception as exc:  # an agent's check is untrusted code: any error is a FAIL
        message = f"{CHECK_RAISED}: {type(exc).__name__}: {exc}"
        return CheckResult(ok=False, message=message), True
    if not isinstance(result, CheckResult):
        message = f"{CHECK_RAISED}: returned {type(result).__name__}, not a CheckResult"
        return CheckResult(ok=False, message=message), True
    return result, False
