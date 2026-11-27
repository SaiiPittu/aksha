"""A check that raises is a FAIL with ``check_raised``, never a crash (ADR-P15 D2).

A ``KeyError`` (a field the saved work lacks) is an ordinary failure naming the field (S7).
"""

from __future__ import annotations

from typing import Any

from aksha.runtime.tasks.checks import check_name, run_check
from aksha.runtime.tasks.types import CheckResult


def passes(view: Any) -> CheckResult:
    return CheckResult(ok=True)


def boom(view: Any) -> CheckResult:
    raise RuntimeError("bug")


def missing(view: Any) -> CheckResult:
    return CheckResult(ok=bool({"rows": []}["cells"]))


def wrong(view: Any) -> Any:
    return True


def test_a_passing_check_is_returned_as_is() -> None:
    assert run_check(passes, None) == (CheckResult(ok=True), False)


def test_a_raising_check_is_a_failing_result() -> None:
    result, raised = run_check(boom, None)
    assert raised and not result.ok
    assert result.message == "check_raised: RuntimeError: bug"


def test_a_missing_field_is_a_plain_failure_naming_the_field() -> None:
    result, raised = run_check(missing, None)
    assert not raised and not result.ok
    assert "check_raised" not in result.message
    assert result.message.startswith("missing: the saved work has no field 'cells'")


def test_a_check_returning_something_else_is_a_failing_result() -> None:
    result, raised = run_check(wrong, None)
    assert raised and not result.ok
    assert "not a CheckResult" in result.message


def test_check_names_are_qualified_names() -> None:
    assert check_name(passes) == "passes"
    assert check_name(object()) == "check"
