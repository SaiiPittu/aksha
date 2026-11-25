"""``TaskView``: what a completion check may read (ADR-P15 D2, D4)."""

from __future__ import annotations

from aksha.runtime.tasks.scheduler import entered_fact
from aksha.runtime.tasks.view import TaskView
from tests.support.tasks import TaskLog, demo_tree, ref


def test_latest_and_own_read_current_versions_by_local_kind() -> None:
    tree, log = demo_tree(), TaskLog()
    view = TaskView(log.view(), tree, ref("extract", "C1"))
    assert view.key == "C1" and view.own("item") is None
    log.save("item", "C1", {"v": 1})
    log.save("item", "C1", {"v": 2})
    log.save("item", "C2", {"v": 9})
    view = TaskView(log.view(), tree, ref("extract", "C1"))
    own = view.own("item")
    assert own is not None and own.content["v"] == 2 and own.ref.version == 2
    assert view.latest("demo.item", "C2") is not None
    assert {k: v.content["v"] for k, v in view.all_latest("item").items()} == {"C1": 2, "C2": 9}


def test_a_pending_revision_is_not_current() -> None:
    tree, log = demo_tree(), TaskLog()
    log.save("cmp", "C1", {"v": 1})
    log.save("cmp", "C1", {"v": 2})
    log.add(
        "interrupt.raised", {"interrupt": "I-1", "kind": "review", "revision": "demo.cmp/C1@v2"}
    )
    view = TaskView(log.view(), tree, ref("compare", "C1"))
    own = view.own("cmp")
    assert own is not None and own.ref.version == 1
    assert view.all_latest("cmp")["C1"].ref.version == 1


def test_answers_are_the_resolutions_of_this_instances_interrupts() -> None:
    tree, log = demo_tree(), TaskLog()
    log.raise_interrupt("extract[C1]", "I-1")
    log.raise_interrupt("extract[C2]", "I-2")
    log.resolve("I-2", "no")
    log.resolve("I-1", "yes")
    view = TaskView(log.view(), tree, ref("extract", "C1"))
    assert [a["answer"] for a in view.answers()] == ["yes"]


def test_child_status_reads_the_child_instance_under_this_key() -> None:
    from aksha.runtime.tasks.tree import compile_tree
    from aksha.runtime.tasks.types import Task, source_keys
    from tests.support.tasks import AGENT, has

    tree = compile_tree(
        AGENT,
        (Task("collect", per=source_keys("notes"), children=(Task("read", done=has("a")),)),),
    )
    log = TaskLog()
    log.add("keys.listed", {"collection": "notes", "keys": ["n1"]})
    log.commit([entered_fact(tree, ref("collect/read", "n1"), cause="scheduler")])
    view = TaskView(log.view(), tree, ref("collect", "n1"))
    assert dict(view.child_status("read")) == {"n1": "running"}


def test_freshness_reads_the_invalidation_view() -> None:
    tree, log = demo_tree(), TaskLog()
    log.save("item", "C1", {"v": 1})
    view = TaskView(log.view(), tree, ref("extract", "C1"))
    assert view.freshness("demo.item/C1@v1") == "current"
    assert view.freshness("demo.item/C9@v1") is None


def test_a_view_holds_no_store_clock_or_model() -> None:
    tree, log = demo_tree(), TaskLog()
    view = TaskView(log.view(), tree, ref("scope"))
    assert set(vars(view)) == {"log", "tree", "instance"}
