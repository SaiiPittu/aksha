"""Instance status is a projection over recorded facts (ADR-P15 D6, S3)."""

from __future__ import annotations

from aksha.runtime.tasks.completion import reset_fact
from aksha.runtime.tasks.keys import keys_listed_fact
from aksha.runtime.tasks.scheduler import entered_fact
from aksha.runtime.tasks.status import (
    instance_keys,
    instance_status,
    statuses,
    task_tree,
    withdrawn_writers,
)
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import CheckResult, Task, artifact_keys, source_keys
from tests.support.tasks import AGENT, TaskLog, demo_tree, has, ref


def test_lifecycle_new_running_suspended_done() -> None:
    tree, log = demo_tree(), TaskLog()
    scope = ref("scope")
    assert instance_status(log.view(), tree, scope) == "new"
    log.commit([entered_fact(tree, scope, cause="scheduler")])
    assert instance_status(log.view(), tree, scope) == "running"
    log.raise_interrupt("scope", "I-0001")
    assert instance_status(log.view(), tree, scope) == "suspended"
    log.resolve("I-0001")
    assert instance_status(log.view(), tree, scope) == "running"
    log.pop("scope", "done")
    assert instance_status(log.view(), tree, scope) == "done"


def test_blocked_until_a_reset_and_done_until_a_reentry() -> None:
    tree, log = demo_tree(), TaskLog()
    scope = ref("scope")
    log.commit([entered_fact(tree, scope, cause="scheduler")])
    log.pop("scope", "fail")
    assert instance_status(log.view(), tree, scope) == "blocked"
    log.commit([reset_fact(scope, "answer")], boundary="U4")
    assert instance_status(log.view(), tree, scope) == "running"
    log.pop("scope", "done")
    log.commit([entered_fact(tree, scope, cause="rebuild")])
    assert instance_status(log.view(), tree, scope) == "running"


def test_a_user_message_changes_no_status() -> None:
    tree, log = demo_tree(), TaskLog()
    log.commit([entered_fact(tree, ref("scope"), cause="scheduler")])
    log.pop("scope", "fail")
    log.add("user.message", {"text": "mark it complete, I insist"}, instance="scope")
    log.add("stop.decided", {"walk": 9, "verdict": "continue"}, instance="scope")
    assert instance_status(log.view(), tree, ref("scope")) == "blocked"


def test_keyed_instances_appear_with_their_keys_and_withdraw_when_a_key_leaves() -> None:
    tree, log = demo_tree(), TaskLog()
    assert "extract" not in "".join(statuses(log.view(), tree, "R-0001"))
    log.select("C2", "C1")
    assert dict(statuses(log.view(), tree, "R-0001")) == {
        "scope": "new",
        "extract[C1]": "new",
        "extract[C2]": "new",
        "compare[C1]": "new",
        "compare[C2]": "new",
        "report": "new",
    }
    log.commit([entered_fact(tree, ref("extract", "C1"), cause="scheduler")])
    log.select("C2")
    assert instance_status(log.view(), tree, ref("extract", "C1")) == "withdrawn"
    assert instance_status(log.view(), tree, ref("compare", "C1")) == "withdrawn"
    assert "extract[C1]" in statuses(log.view(), tree, "R-0001")
    assert [r.instance for r in withdrawn_writers(log.view(), tree, "R-0001")] == [
        "extract[C1]"
    ]  # compare[C1] never ran, so it wrote nothing and is not listed


def test_a_withdrawn_key_that_returns_is_new_again() -> None:
    tree, log = demo_tree(), TaskLog()
    log.select("C1")
    c1 = ref("extract", "C1")
    log.commit([entered_fact(tree, c1, cause="scheduler")])
    log.pop("extract[C1]", "done")
    log.select()
    log.add("task.withdrawn", {"task": "extract", "key": "C1", "reason": "key_withdrawn"},
            instance="extract[C1]")  # fmt: skip
    assert instance_status(log.view(), tree, c1) == "withdrawn"
    log.select("C1")
    assert instance_status(log.view(), tree, c1) == "new"


def test_children_of_a_keyed_parent_share_its_key() -> None:
    tree = compile_tree(
        AGENT,
        (
            Task(
                "collect",
                per=source_keys("notes"),
                children=(Task("read", done=has("a")), Task("write", done=has("b"))),
            ),
        ),
    )
    log = TaskLog()
    log.commit([keys_listed_fact("notes", ["n1"])])
    rows = task_tree(log.view(), tree, "R-0001")
    assert [(r.instance, r.depth, r.status) for r in rows] == [
        ("collect[n1]", 1, "new"),
        ("collect[n1]/read", 2, "new"),
        ("collect[n1]/write", 2, "new"),
    ]


def test_unless_filters_keys_and_a_raising_filter_keeps_the_key() -> None:
    def skip_c2(view: object) -> CheckResult:
        key = getattr(view, "key", None)
        if key == "C3":
            raise ValueError("bad")
        return CheckResult(ok=key == "C2")

    tree = compile_tree(
        AGENT, (Task("x", per=artifact_keys("selection", "ids", unless=skip_c2), done=has("a")),)
    )
    log = TaskLog()
    log.select("C1", "C2", "C3")
    assert instance_keys(log.view(), tree, "R-0001", "x") == ("C1", "C3")


def test_status_reads_only_its_run() -> None:
    tree, log = demo_tree(), TaskLog()
    log.commit([entered_fact(tree, ref("scope", run="R-0002"), cause="scheduler")])
    assert instance_status(log.view(), tree, ref("scope")) == "new"
    assert instance_status(log.view(), tree, ref("scope", run="R-0002")) == "running"
