"""The four opt-in stop policies (ADR-P16 D4) and how the stack composes what they add."""

from __future__ import annotations

from typing import Any

from aksha.runtime.budgets.limits import vetoes_since_entry
from aksha.runtime.policy.types import StopPolicyNarrowing
from aksha.runtime.stop_policy.builtins import (
    ForcedToolPolicy,
    GoalPolicy,
    PlanPolicy,
    TodoReminderPolicy,
    build,
)
from aksha.runtime.stop_policy.stack import compose_prep, derive_stack, narrowing_of, prep_for
from aksha.runtime.stop_policy.types import (
    ForcedTool,
    Goal,
    Plan,
    Prep,
    StopCandidate,
    StopView,
    TodoReminder,
)
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import CheckResult, Task
from aksha.runtime.tasks.view import TaskView
from tests.support.budgets import RUN
from tests.support.stop import decide, view_of
from tests.support.tasks import AGENT, TaskLog, has, ref


def green_suite(view: TaskView) -> CheckResult:
    report = view.latest("test_report")
    ok = report is not None and report.content.get("failed") == 0
    return CheckResult(ok=ok, message="tests not run on current files")


def plan_ok(view: TaskView) -> CheckResult:
    plan = view.own("plan")
    return CheckResult(ok=plan is not None and bool(plan.content.get("steps")), message="no steps")


def always(view: Any) -> CheckResult:
    return CheckResult(ok=True)


def never(view: Any) -> CheckResult:
    return CheckResult(ok=False, message="not yet")


def _fix(*policies: Any, brief: str | None = None, children: tuple[Task, ...] = ()) -> Any:
    task = Task(
        "fix",
        writes=("patch", "plan", "test_report"),
        done=has("patch") if not children else None,
        stop_policies=policies,
        brief=brief,
        children=children,
    )
    return compile_tree(AGENT, (task,))


def _log() -> TaskLog:
    log = TaskLog()
    log.started()
    return log


def _finish(after: int) -> StopCandidate:
    return StopCandidate(RUN, "finish", ref("fix"), after_turn=after)


def test_goal_vetoes_a_finish_until_its_check_passes() -> None:
    tree, log = _fix(Goal(green_suite)), _log()
    decide(log, tree, StopCandidate(RUN, "next"))
    log.save("patch", None, {"diff": "x"})
    first = decide(log, tree, _finish(1))  # the model says done before running tests
    assert first.verdict.kind == "continue"
    assert first.decided_by == "goal:fix"
    assert first.verdict.reason == "tests not run on current files"
    assert vetoes_since_entry(log.view(), RUN, "fix") == 1  # a goal veto counts
    log.save("test_report", None, {"failed": 0})
    second = decide(log, tree, _finish(2))
    assert second.pops == (("fix", "done"),)
    assert second.trail[:2] == (("goal:fix", "pass"), ("task:fix", "done"))


def test_goal_vetoes_count_toward_the_retry_cap() -> None:
    tree, log = _fix(Goal(never)), _log()
    decide(log, tree, StopCandidate(RUN, "next"))
    log.save("patch", None, {"diff": "x"})
    decide(log, tree, _finish(1))
    decide(log, tree, _finish(2))
    third = decide(log, tree, _finish(3))
    assert third.limit == "stop_veto_limit"
    assert third.replaced == ("goal:fix", "continue")
    assert third.pops == (("fix", "fail"),)


def test_plan_mode_narrows_tools_to_the_plan_save_until_the_plan_is_accepted() -> None:
    tree, log = _fix(Plan("plan", plan_ok)), _log()
    decide(log, tree, StopCandidate(RUN, "next"))
    view = view_of(log, tree)
    prep = prep_for(view, ref("fix"))
    assert prep.tools == frozenset({"save_artifact"})
    assert prep.narrowed_by == "plan:fix"
    assert narrowing_of(prep) == StopPolicyNarrowing(frozenset({"save_artifact"}), "plan:fix")
    assert any("Write the plan first" in line for line in prep.brief)
    vetoed = decide(log, tree, _finish(1))
    assert vetoed.decided_by == "plan:fix" and vetoed.verdict.kind == "continue"
    log.save("plan", None, {"steps": ["edit", "test"]})
    assert prep_for(view_of(log, tree), ref("fix")).tools is None


def test_forced_tool_narrows_to_one_tool_while_its_condition_holds() -> None:
    tree, log = _fix(ForcedTool("run_tests", always)), _log()
    decide(log, tree, StopCandidate(RUN, "next"))
    prep = prep_for(view_of(log, tree), ref("fix"))
    assert prep.tools == frozenset({"run_tests"})
    assert prep.tool_choice == "run_tests"
    quiet = _fix(ForcedTool("run_tests", never))
    assert prep_for(view_of(log, quiet), ref("fix")) == Prep()
    d = decide(log, tree, StopCandidate(RUN, "tools", ref("fix"), after_turn=1))
    assert ("forced_tool:fix", "pass") in d.trail  # it never decides


def test_todo_reminder_lists_open_todos_every_n_turns_and_never_vetoes() -> None:
    tree, log = _fix(TodoReminder(every=2)), _log()
    decide(log, tree, StopCandidate(RUN, "next"))  # turn 1
    assert prep_for(view_of(log, tree), ref("fix")).brief == ()
    decide(log, tree, StopCandidate(RUN, "tools", ref("fix"), after_turn=1))  # turn 2
    brief = prep_for(view_of(log, tree), ref("fix")).brief
    assert brief == ("Open todos: save patch first",)
    d = decide(log, tree, StopCandidate(RUN, "tools", ref("fix"), after_turn=2))
    assert ("todo_reminder:fix", "pass") in d.trail


def test_todo_reminder_on_a_parent_lists_its_unsettled_children() -> None:
    children = (Task("edit", writes=("patch",), done=has("patch")),)
    tree, log = _fix(TodoReminder(every=1), children=children), _log()
    view = view_of(log, tree)
    reminder = TodoReminderPolicy(ref("fix"), 1)
    assert reminder.todos(view) == ("fix/edit: save patch first",)


def test_the_stack_is_derived_outer_to_inner_with_agent_policies_outside_the_tasks() -> None:
    tree, log = _fix(Plan("plan", plan_ok), Goal(green_suite)), _log()
    decide(log, tree, StopCandidate(RUN, "next"))
    view: StopView = view_of(log, tree, agent_policies=(TodoReminder(every=3),))
    stack = derive_stack(view, StopCandidate(RUN, "tools", ref("fix")))
    assert [p.ref for p in stack] == [
        "run:R-0001", "task:fix", "todo_reminder:fix", "plan:fix", "goal:fix",
    ]  # fmt: skip


def test_compose_prep_concatenates_briefs_outer_first_and_never_widens_tools() -> None:
    tree = _fix(Plan("plan", plan_ok), ForcedTool("run_tests", always), brief="Fix it.")
    log = _log()
    decide(log, tree, StopCandidate(RUN, "next"))
    view = view_of(log, tree)
    stack = derive_stack(view, StopCandidate(RUN, "tools", ref("fix")))
    prep = compose_prep(stack, view)
    assert prep.brief[0] == "Fix it."
    assert prep.tools == frozenset()  # plan keeps the save, the forced tool keeps run_tests
    assert prep.tool_choice is None  # a hint outside the kept tools is dropped


def test_a_policy_whose_prepare_raises_fails_closed() -> None:
    def boom(view: Any) -> CheckResult:
        raise RuntimeError("bug")

    tree, log = _fix(Plan("plan", plan_ok)), _log()
    log.save("plan", None, {"steps": ["x"]})
    decide(log, tree, StopCandidate(RUN, "next"))
    broken = PlanPolicy(ref("fix"), "plan", boom)
    view = view_of(log, tree)
    assert broken.accepted(view) is False  # a raising accept rule is not accepted
    prep = compose_prep((broken,), view)
    assert prep.tools == frozenset({"save_artifact"})


def test_build_turns_each_opt_in_into_its_policy() -> None:
    r = ref("fix")
    assert isinstance(build(Goal(always), r), GoalPolicy)
    assert isinstance(build(Plan("plan", always), r), PlanPolicy)
    assert isinstance(build(ForcedTool("t", always), r), ForcedToolPolicy)
    assert build(TodoReminder(every=4), r) == TodoReminderPolicy(r, 4)
