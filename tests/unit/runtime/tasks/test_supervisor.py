"""The supervisor rule over hand-built views, one per branch (D7, ``test_supervisor_turn_rule``)."""

from __future__ import annotations

from typing import Any

from aksha.runtime.tasks.keys import keys_listed_fact
from aksha.runtime.tasks.scheduler import entered_fact, runnable
from aksha.runtime.tasks.status import run_state
from aksha.runtime.tasks.supervisor import is_supervisor, supervisor_step
from aksha.runtime.tasks.tree import CompiledTree, compile_tree
from aksha.runtime.tasks.types import CheckResult, SupervisorMove, Task
from tests.support.tasks import AGENT, TaskLog, has_own, ref

RUN = "R-0001"


def steered(view: Any) -> CheckResult:
    """Done once a directive is saved; a failing check the supervisor clears itself."""
    ok = view.latest("directive") is not None
    return CheckResult(ok=ok, message="save a directive", clears="supervisor")


def waits_on_person(view: Any) -> CheckResult:
    return CheckResult(ok=False, message="a person decides", clears="person")


def tree_with(done: Any = steered, wakes_on: tuple[str, ...] = ("source.changed",)) -> CompiledTree:
    return compile_tree(
        AGENT,
        (
            Task(
                "lead",
                done=done,
                wakes_on=wakes_on,
                children=(Task("work", done=has_own("out")),),
            ),
        ),
    )


def entered(log: TaskLog, tree: CompiledTree) -> None:
    log.commit([entered_fact(tree, ref("lead"), cause="scheduler")])


def step(log: TaskLog, tree: CompiledTree, **limits: int) -> SupervisorMove:
    state = run_state(log.view(), tree, RUN)
    below = tuple(r for r in runnable(log.view(), tree, RUN) if r.task_path != "lead")
    return supervisor_step(state, ref("lead"), below, **limits)


def test_is_supervisor_needs_children_and_wakes_on_or_a_brief() -> None:
    assert is_supervisor(Task("a", wakes_on=("x.y",), children=(Task("b"),)))
    assert is_supervisor(Task("a", brief="p.md", children=(Task("b"),)))
    assert not is_supervisor(Task("a", children=(Task("b"),)))
    assert not is_supervisor(Task("a", wakes_on=("x.y",)))


def test_a_wake_event_since_its_last_turn_gives_a_turn() -> None:
    tree, log = tree_with(), TaskLog()
    entered(log, tree)
    log.add("source.changed", {"source_version": 1, "cause": "command", "sources": {},
                               "records": {}})  # fmt: skip
    assert step(log, tree) == SupervisorMove("turn", "wake")
    log.decided("continue", instance="lead", turn=1)
    assert step(log, tree).move == "push"


def test_otherwise_it_pushes_its_first_runnable_child() -> None:
    tree, log = tree_with(), TaskLog()
    entered(log, tree)
    move = step(log, tree)
    assert (move.move, move.child) == ("push", ref("lead/work"))
    assert [r.instance for r in runnable(log.view(), tree, RUN)] == ["lead/work"]


def test_done_when_its_children_settled_and_its_check_passes() -> None:
    tree, log = tree_with(), TaskLog()
    entered(log, tree)
    log.pop("lead/work")
    log.save("directive", None, {"focus": "a"})
    assert step(log, tree) == SupervisorMove("done")
    assert [r.instance for r in runnable(log.view(), tree, RUN)] == ["lead"]


def test_a_failing_check_tagged_supervisor_gives_it_a_turn() -> None:
    tree, log = tree_with(), TaskLog()
    entered(log, tree)
    log.pop("lead/work")
    assert step(log, tree) == SupervisorMove("turn", "supervisor")


def test_a_failing_check_for_a_person_holds() -> None:
    tree, log = tree_with(done=waits_on_person), TaskLog()
    entered(log, tree)
    log.pop("lead/work")
    assert step(log, tree) == SupervisorMove("hold", "person")
    assert runnable(log.view(), tree, RUN) == ()


def test_the_activation_limit_bites_before_a_turn() -> None:
    tree, log = tree_with(), TaskLog()
    entered(log, tree)
    log.pop("lead/work")
    for turn in (1, 2):
        log.decided("continue", instance="lead", turn=turn)
        log.save("note", None, {"n": turn}, instance="lead/work")
    assert step(log, tree, activation_limit=2) == SupervisorMove("fail", "activation_limit")
    assert step(log, tree, activation_limit=5).move == "turn"


def test_the_no_progress_limit_counts_turns_since_a_child_progressed() -> None:
    tree, log = tree_with(), TaskLog()
    entered(log, tree)
    log.pop("lead/work")
    for turn in (1, 2, 3):
        log.decided("continue", instance="lead", turn=turn)
    assert step(log, tree) == SupervisorMove("fail", "no_progress_limit")
    log.save("out", None, {"n": 1}, instance="lead/work")
    assert step(log, tree).move == "turn"


def test_a_keyed_supervisor_steers_children_per_key() -> None:
    tree = compile_tree(
        AGENT,
        (
            Task(
                "lead",
                per=keys_source(),
                brief="lead.md",
                done=steered,
                children=(Task("work", done=has_own("out")),),
            ),
        ),
    )
    log = TaskLog()
    log.commit([keys_listed_fact("notes", ["a"])])
    log.commit([entered_fact(tree, ref("lead", "a"), cause="scheduler")])
    assert [r.instance for r in runnable(log.view(), tree, RUN)] == ["lead[a]/work"]


def keys_source() -> Any:
    from aksha.runtime.tasks.types import source_keys

    return source_keys("notes")


# --- an un-keyed supervisor over keyed workers (the portfolio shape, D7 amendment) --------


def fleet() -> CompiledTree:
    """``lead`` (un-keyed, a brief) over ``work``, keyed by the ``items`` listing."""
    from aksha.runtime.tasks.types import artifact_keys

    return compile_tree(
        AGENT,
        (
            Task(
                "lead",
                brief="lead.md",
                done=steered,
                children=(Task("work", per=artifact_keys("items", "keys"), done=has_own("out")),),
            ),
        ),
    )


def test_a_keyed_worker_under_an_unkeyed_supervisor_has_one_instance_per_key() -> None:
    tree, log = fleet(), TaskLog()
    log.save("items", None, {"keys": ["b", "a"]})
    entered(log, tree)
    assert [r.instance for r in runnable(log.view(), tree, RUN)] == ["lead[a]/work", "lead[b]/work"]
    move = step(log, tree)
    assert (move.move, move.child) == ("push", ref("lead/work", "a"))


def test_the_supervisor_sees_each_workers_status_by_key() -> None:
    from aksha.runtime.tasks.view import TaskView

    tree, log = fleet(), TaskLog()
    log.save("items", None, {"keys": ["a", "b"]})
    entered(log, tree)
    log.save("out", "a", {"n": 1}, instance="lead[a]/work")
    log.pop("lead[a]/work")
    view = TaskView(log.view(), tree, ref("lead"))
    assert dict(view.child_status("work")) == {"a": "done", "b": "new"}


def test_a_keyed_workers_save_is_progress_for_the_no_progress_limit() -> None:
    tree, log = fleet(), TaskLog()
    log.save("items", None, {"keys": ["a"]})
    entered(log, tree)
    log.save("out", "a", {"n": 1}, instance="lead[a]/work")
    log.pop("lead[a]/work")
    for turn in (1, 2, 3):
        log.decided("continue", instance="lead", turn=turn)
    assert step(log, tree) == SupervisorMove("fail", "no_progress_limit")
    log.save("out", "a", {"n": 2}, instance="lead[a]/work")
    assert step(log, tree).move == "turn"


def test_spent_counts_turns_per_key_below_the_supervisor_and_not_its_own() -> None:
    from aksha.runtime.tasks.view import TaskView

    tree, log = fleet(), TaskLog()
    log.save("items", None, {"keys": ["a", "b"]})
    entered(log, tree)
    log.decided("continue", instance="lead", turn=1)
    for turn in (1, 2):
        log.decided("continue", instance="lead[a]/work", turn=turn)
    log.decided("continue", instance="lead[b]/work", turn=1)
    view = TaskView(log.view(), tree, ref("lead"))
    assert view.spent("a")["model_turns"] == 2
    assert view.spent("b")["model_turns"] == 1
    assert view.spent()["model_turns"] == 3


def test_spent_below_a_keyed_instance_counts_only_its_own_key() -> None:
    from aksha.runtime.tasks.types import artifact_keys
    from aksha.runtime.tasks.view import TaskView

    per = artifact_keys("items", "keys")
    tree = compile_tree(
        AGENT, (Task("lead", per=per, children=(Task("work", done=has_own("o")),)),)
    )
    log = TaskLog()
    log.save("items", None, {"keys": ["a", "b"]})
    log.decided("continue", instance="lead[a]/work", turn=1)
    for turn in (1, 2):
        log.decided("continue", instance="lead[b]/work", turn=turn)
    assert TaskView(log.view(), tree, ref("lead", "a")).spent()["model_turns"] == 1
    assert TaskView(log.view(), tree, ref("lead", "b")).spent()["model_turns"] == 2


def test_a_keyed_task_below_a_keyed_task_is_still_refused() -> None:
    import pytest

    from aksha.runtime.tasks.errors import TreeInvalid
    from aksha.runtime.tasks.types import artifact_keys

    per = artifact_keys("items", "keys")
    lead = Task("lead", per=per, brief="b", children=(Task("work", per=per, done=has_own("o")),))
    with pytest.raises(TreeInvalid, match="keyed_child_unsupported"):
        compile_tree(AGENT, (lead,))
