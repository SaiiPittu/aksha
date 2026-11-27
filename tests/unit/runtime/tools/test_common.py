"""``todo`` and ``think``: reads with no effect; a plan with two items in progress is refused."""

from __future__ import annotations

from aksha.runtime.tools.common import think_tool, todo_tool
from aksha.runtime.tools.types import Err, Ok, ToolContext

CTX = ToolContext("o", "a", "t", None, "s", 1)


def _todo(items: list[dict[str, str]]) -> Ok | Err:
    decl = todo_tool("a")
    assert decl.body is not None
    return decl.body(CTX, {"todos": items}).result


def test_todo_echoes_the_plan_with_counts() -> None:
    got = _todo([{"content": "read the notes", "status": "done"},
                 {"content": "compare  prices", "status": "in_progress"},
                 {"content": "save", "status": "pending"}])  # fmt: skip
    assert isinstance(got, Ok) and got.spans == () and got.reads == ()
    assert got.data["counts"] == {"pending": 1, "in_progress": 1, "done": 1}
    assert got.data["todos"][1]["content"] == "compare prices"


def test_todo_refuses_two_items_in_progress_naming_both() -> None:
    got = _todo(
        [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}]
    )
    assert isinstance(got, Err) and got.code == "todo_invalid"
    assert got.paths == ("/todos/0/status", "/todos/1/status")


def test_todo_refuses_an_item_named_twice() -> None:
    got = _todo([{"content": "Read", "status": "done"}, {"content": "read", "status": "pending"}])
    assert isinstance(got, Err) and got.paths == ("/todos/1/content",)


def test_both_are_reads_with_no_collection_and_think_changes_nothing() -> None:
    for decl in (todo_tool("a"), think_tool("a")):
        assert decl.spec.effect == "read" and decl.spec.collections == frozenset()
        assert not decl.spec.mints_citation_spans and decl.spec.egress is None
    decl = think_tool("a")
    assert decl.body is not None
    got = decl.body(CTX, {"thought": "two pages left"}).result
    assert isinstance(got, Ok) and got.data == {"noted": True}
