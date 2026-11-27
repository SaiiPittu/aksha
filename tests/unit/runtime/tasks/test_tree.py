"""The compiled Task tree and its v1 refusals (ADR-P15 D1)."""

from __future__ import annotations

import pytest

from aksha.runtime.tasks.errors import TaskUnknown, TreeInvalid
from aksha.runtime.tasks.tree import compile_tree, state_table
from aksha.runtime.tasks.types import Read, Task, source_keys
from tests.support.tasks import AGENT, demo_tree, has


def test_nodes_are_compiled_by_path_with_depth_and_parent() -> None:
    tree = compile_tree(
        AGENT,
        (
            Task(
                "collect",
                per=source_keys("notes"),
                children=(Task("read", done=has("x")), Task("write", done=has("y"))),
            ),
            Task("sum", needs=("collect",), done=has("z")),
        ),
    )
    assert tree.roots == ("collect", "sum")
    assert [n.path for n in tree.walk()] == ["collect", "collect/read", "collect/write", "sum"]
    write = tree.node("collect/write")
    assert (write.depth, write.parent, write.top, write.is_leaf) == (2, "collect", "collect", True)
    assert tree.node("collect").children == ("collect/read", "collect/write")
    assert tree.key_source("collect/write") == source_keys("notes")
    assert tree.key_source("sum") is None
    assert tree.siblings("collect/read") == ("collect/read", "collect/write")
    assert tree.siblings("sum") == ("collect", "sum")


def test_kinds_resolve_in_the_agents_namespace() -> None:
    tree = compile_tree(AGENT, (Task("a", reads=("line", Read("x.y", keys="all")), done=has("l")),))
    assert tree.kind("line") == "demo.line"
    assert tree.kind("other.kind") == "other.kind"
    assert tree.reads("a") == (Read("demo.line"), Read("x.y", keys="all"))


def test_unknown_path_is_refused() -> None:
    with pytest.raises(TaskUnknown) as e:
        demo_tree().node("nope")
    assert e.value.code == "task_unknown"


def test_compile_refuses_every_problem_not_the_first() -> None:
    tasks = (
        Task(
            "a",
            per=source_keys("notes"),
            children=(Task("b", per=source_keys("notes"), done=has("x")),),
        ),
        Task("c"),
    )
    with pytest.raises(TreeInvalid) as e:
        compile_tree(AGENT, tasks, concurrency=2)
    codes = [code for code, _, _ in e.value.problems]
    assert codes == ["concurrency_unsupported", "keyed_child_unsupported", "leaf_without_check"]
    assert e.value.code == "concurrency_unsupported"


def test_duplicate_siblings_are_refused() -> None:
    with pytest.raises(TreeInvalid) as e:
        compile_tree(AGENT, (Task("a", done=has("x")), Task("a", done=has("x"))))
    assert e.value.code == "duplicate_task"


def test_state_table_prints_the_tree_in_pick_order() -> None:
    table = state_table(demo_tree())
    lines = table.splitlines()
    assert lines[0].startswith("| Task | Fan-out |")
    assert [line.split("|")[1].strip() for line in lines[2:]] == [
        "`scope`",
        "`extract`",
        "`compare`",
        "`report`",
    ]
    assert "per ids of selection" in lines[3]
    assert "has_own_item" in lines[3]
    assert "| extract |" in lines[4]
