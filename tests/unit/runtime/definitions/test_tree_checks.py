from typing import Any

from aksha.runtime.definitions.tree_checks import (
    MAX_TASK_DEPTH,
    TreeContext,
    check_tree,
    is_builtin_opt_in,
    is_function,
    is_granted,
    reads_of,
    stop_policy_message,
    walk_tasks,
)
from aksha.runtime.stop_policy.types import StopPolicyOptIn, TodoReminder
from aksha.runtime.tasks.types import CheckResult, Read, Task


def _ok(view: Any) -> CheckResult:
    return CheckResult(ok=True)


TREE = (Task("a", children=(Task("b", done=_ok), Task("c", done=_ok))), Task("d", done=_ok))


def _ctx(**changes: Any) -> TreeContext:
    base: dict[str, Any] = dict(
        module="m/agent.py", namespace="m", agent_tools=frozenset(), tool_decls={},
        own_kinds=frozenset(), readable_kinds=frozenset(), written_or_derived=frozenset(),
        extension_names=frozenset(), granted=(), aliases=frozenset(),
    )  # fmt: skip
    return TreeContext(**{**base, **changes})


def test_walk_tasks_is_depth_first_with_paths() -> None:
    assert [p for p, _ in walk_tasks(TREE)] == ["a", "a/b", "a/c", "d"]


def test_reads_of_turns_strings_into_own_reads() -> None:
    task = Task("x", reads=("m.k", Read("m.j", keys="all")))
    assert reads_of(task) == (Read("m.k"), Read("m.j", keys="all"))


def test_is_granted_by_full_name_or_namespace() -> None:
    assert is_granted("o.k", ("o.*",)) and is_granted("o.k", ("o.k",))
    assert not is_granted("o.k", ("p.*", "o.j"))


def test_is_function_and_opt_in_helpers() -> None:
    assert is_function(_ok) and not is_function("x")
    assert is_builtin_opt_in(TodoReminder(every=1))
    assert not is_builtin_opt_in(StopPolicyOptIn("custom", {}))  # type: ignore[arg-type]
    assert "'custom' is not a built-in" in stop_policy_message(StopPolicyOptIn("custom", {}))  # type: ignore[arg-type]


def test_check_tree_accepts_a_sound_tree_and_bounds_depth() -> None:
    assert check_tree(TREE, _ctx()) == []
    deep = Task("n1", children=(Task("n2", children=(Task("n3", children=(
        Task("n4", children=(Task("n5", done=_ok),)),)),)),))  # fmt: skip
    (refusal,) = check_tree((deep,), _ctx())
    assert refusal.code == "tree_too_deep" and refusal.task == "n1/n2/n3/n4/n5"
    assert MAX_TASK_DEPTH == 4


def test_a_task_named_like_a_step_id_segment_is_refused() -> None:
    tree = (
        Task("t03", done=_ok),
        Task("a", children=(Task("end", done=_ok),)),
        Task("tool", done=_ok),
    )
    refusals = check_tree(tree, _ctx())
    assert [(r.code, r.task, r.field) for r in refusals] == [
        ("task_id_reserved", "t03", "id"),
        ("task_id_reserved", "a/end", "id"),
    ]
