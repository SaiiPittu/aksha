from aksha.runtime.stop_policy.types import (
    BUILTIN_STOP_POLICIES,
    ForcedTool,
    Goal,
    Plan,
    Recite,
    StopPolicyOptIn,
    TodoReminder,
)
from aksha.runtime.tasks.types import CheckResult


def _ok(view: object) -> CheckResult:
    return CheckResult(ok=True)


def _lines(view: object) -> tuple[str, ...]:
    return ("a line",)


def test_the_built_in_set_is_closed_and_has_five_members() -> None:
    assert frozenset({"goal", "plan", "forced_tool", "todo_reminder", "recite"}) == (
        BUILTIN_STOP_POLICIES
    )


def test_each_constructor_records_its_parameters() -> None:
    assert Goal(_ok) == StopPolicyOptIn("goal", {"check": _ok})
    assert Plan("x.plan", _ok) == StopPolicyOptIn("plan", {"kind": "x.plan", "accept": _ok})
    assert ForcedTool("save", _ok) == StopPolicyOptIn("forced_tool", {"tool": "save", "when": _ok})
    assert TodoReminder(every=2) == StopPolicyOptIn("todo_reminder", {"every": 2})
    assert Recite(_lines, max_chars=9) == StopPolicyOptIn(
        "recite", {"render": _lines, "max_chars": 9}
    )


def test_opt_in_params_are_read_only() -> None:
    import pytest

    opt = TodoReminder(every=2)
    with pytest.raises(TypeError):
        opt.params["every"] = 3  # type: ignore[index]
