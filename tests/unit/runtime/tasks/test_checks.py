"""A check that raises is a FAIL with ``check_raised``, never a crash (ADR-P15 D2)."""

from __future__ import annotations

from typing import Any

from aksha.runtime.tasks.checks import check_name, run_check
from aksha.runtime.tasks.types import CheckResult


def passes(view: Any) -> CheckResult:
    return CheckResult(ok=True)


def boom(view: Any) -> CheckResult:
    raise KeyError("cells")


def wrong(view: Any) -> Any:
    return True


def test_a_passing_check_is_returned_as_is() -> None:
    assert run_check(passes, None) == (CheckResult(ok=True), False)


def test_a_raising_check_is_a_failing_result() -> None:
    result, raised = run_check(boom, None)
    assert raised and not result.ok
    assert result.message == "check_raised: KeyError: 'cells'"


def test_a_check_returning_something_else_is_a_failing_result() -> None:
    result, raised = run_check(wrong, None)
    assert raised and not result.ok
    assert "not a CheckResult" in result.message


def test_check_names_are_qualified_names() -> None:
    assert check_name(passes) == "passes"
    assert check_name(object()) == "check"
