"""Running a completion check safely: a check that raises is a FAIL, never a crash (D2).

A completion check is the agent's pure function over a ``TaskView``. The runtime calls it
only through :func:`run_check`, which turns an exception into a failing ``CheckResult``
whose message names the exception, and a value that is not a ``CheckResult`` into one
too, so an agent bug blocks one instance instead of ending the process.

A ``KeyError`` is the one exception read as a gap in the saved work rather than a bug: a
check indexed a field the model's content does not have. It is an ordinary failing check
(``check_failed``) whose message names the missing field, so the model can add it in the
next turn, and the instance is not blocked at once (ADR-P15 D2, amendment S7).
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
    except KeyError as exc:  # a field the saved content lacks: the model can add it
        field = exc.args[0] if exc.args else "?"
        message = (
            f"{check_name(check)}: the saved work has no field {field!r}, which this check "
            f"reads: add {field!r} where it is missing and save again"
        )
        return CheckResult(ok=False, message=message), False
    except Exception as exc:  # an agent's check is untrusted code: any error is a FAIL
        message = f"{CHECK_RAISED}: {type(exc).__name__}: {exc}"
        return CheckResult(ok=False, message=message), True
    if not isinstance(result, CheckResult):
        message = f"{CHECK_RAISED}: returned {type(result).__name__}, not a CheckResult"
        return CheckResult(ok=False, message=message), True
    return result, False
