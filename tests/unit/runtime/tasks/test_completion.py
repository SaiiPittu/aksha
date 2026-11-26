"""Completion evaluation: leaf checks, the parent rule, partial parents and vetoes (D2, S1)."""

from __future__ import annotations

from typing import Any

from aksha.runtime.tasks.completion import (
    evaluate_completion,
    evaluated_fact,
    finish_verdict,
    reset_fact,
    task_done,
)
from aksha.runtime.tasks.keys import keys_listed_fact
from aksha.runtime.tasks.scheduler import entered_fact
from aksha.runtime.tasks.status import run_state
from aksha.runtime.tasks.tree import CompiledTree, compile_tree
from aksha.runtime.tasks.types import CheckResult, Task, source_keys
from tests.support.tasks import AGENT, TaskLog, demo_tree, has_own, ref


def boom(view: Any) -> CheckResult:
    raise RuntimeError("no cells")


def parent_tree(on_child_blocked: str = "block", done: Any = None) -> CompiledTree:
    return compile_tree(
        AGENT,
        (
            Task(
                "collect",
                per=source_keys("notes"),
                done=done,
                on_child_blocked=on_child_blocked,  # type: ignore[arg-type]
                children=(Task("read", done=has_own("seen")), Task("write", done=has_own("line"))),
            ),
        ),
    )


def test_a_leaf_passes_on_the_current_artifacts() -> None:
    tree, log = demo_tree(), TaskLog()
    c1 = ref("extract", "C1")
    failed = evaluate_completion(log.view(), tree, c1)
    assert (failed.ok, failed.code, failed.message, failed.check) == (
        False, "check_failed", "save item first", "has_own_item",
    )  # fmt: skip
    log.save("item", "C1", {"v": 1})
    assert evaluate_completion(log.view(), tree, c1).ok


def test_a_raising_check_fails_with_check_raised_and_is_fail_at_once() -> None:
    tree = compile_tree(AGENT, (Task("a", done=boom),))
    log = TaskLog()
    log.commit([entered_fact(tree, ref("a"), cause="scheduler")])
    ev = evaluate_completion(log.view(), tree, ref("a"))
    assert (ev.ok, ev.code) == (False, "check_raised")
    assert finish_verdict(log.view(), tree, ref("a")) == "fail"


def test_three_vetoes_then_blocked() -> None:
    tree, log = demo_tree(), TaskLog()
    scope = ref("scope")
    log.commit([entered_fact(tree, scope, cause="scheduler")])
    assert finish_verdict(log.view(), tree, scope) == "veto"
    log.veto("scope")
    assert finish_verdict(log.view(), tree, scope) == "veto"
    log.veto("scope")
    assert finish_verdict(log.view(), tree, scope) == "fail"
    log.commit([reset_fact(scope, "answer")], boundary="U4")
    assert finish_verdict(log.view(), tree, scope) == "veto"


def test_a_parent_waits_on_its_children_then_its_own_check() -> None:
    tree, log = parent_tree(done=has_own("total")), TaskLog()
    log.commit([keys_listed_fact("notes", ["n1"])])
    parent = ref("collect", "n1")
    ev = evaluate_completion(log.view(), tree, parent)
    assert (ev.code, ev.keys, ev.count) == (
        "children_pending", ("collect[n1]/read", "collect[n1]/write"), 2,
    )  # fmt: skip
    log.pop("collect[n1]/read")
    log.pop("collect[n1]/write")
    ev = evaluate_completion(log.view(), tree, parent)
    assert (ev.ok, ev.code, ev.check) == (False, "check_failed", "has_own_total")
    log.save("total", "n1", {"n": 1})
    assert evaluate_completion(log.view(), tree, parent).ok


def test_a_blocked_child_blocks_the_parent_and_is_named() -> None:
    tree, log = parent_tree(), TaskLog()
    log.commit([keys_listed_fact("notes", ["n1"])])
    log.pop("collect[n1]/read", "fail")
    log.pop("collect[n1]/write")
    ev = evaluate_completion(log.view(), tree, ref("collect", "n1"))
    assert (ev.ok, ev.code, ev.blockers) == (False, "child_blocked", ("collect[n1]/read",))
    assert finish_verdict(log.view(), tree, ref("collect", "n1")) == "fail"


def test_a_partial_parent_finishes_with_blocked_children_named() -> None:
    tree, log = parent_tree("partial"), TaskLog()
    log.commit([keys_listed_fact("notes", ["n1"])])
    log.pop("collect[n1]/read", "fail")
    log.pop("collect[n1]/write")
    ev = evaluate_completion(log.view(), tree, ref("collect", "n1"))
    assert (ev.ok, ev.blockers) == (True, ("collect[n1]/read",))


def test_a_fan_out_as_a_whole_and_s1_partial() -> None:
    log = TaskLog()
    for tree in (demo_tree(), demo_tree(on_child_blocked="partial")):
        assert task_done(run_state(log.view(), tree, "R-0001"), "extract").code == "keys_pending"
    log.select("C1", "C2")
    log.pop("extract[C1]")
    log.pop("extract[C2]", "fail")
    strict = task_done(run_state(log.view(), demo_tree(), "R-0001"), "extract")
    assert (strict.ok, strict.code, strict.blockers) == (False, "child_blocked", ("extract[C2]",))
    partial = task_done(
        run_state(log.view(), demo_tree(on_child_blocked="partial"), "R-0001"), "extract"
    )
    assert (partial.ok, partial.blockers) == (True, ("extract[C2]",))


def test_evaluated_and_reset_facts_are_accepted_in_their_boundaries() -> None:
    tree, log = demo_tree(), TaskLog()
    ev = evaluate_completion(log.view(), tree, ref("extract", "C1"))
    log.commit([evaluated_fact(ref("extract", "C1"), ev, on="finish")])
    log.commit([reset_fact(ref("extract", "C1"), "rebuild")], boundary="U3")
    payload = log.events[-2].payload
    assert payload["check"] == "has_own_item" and payload["key"] == "C1"
    assert log.events[-2].instance == "extract[C1]"
