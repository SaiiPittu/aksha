"""Completion evaluation (ADR-P15 D2): an instance's own check joined with its children's.

Called only by stop-policy evaluation (ADR-P16), on entry and on every finish request; the
facts it returns commit in that stop decision (U3). The rules:

1. A leaf is done when its completion check passes on the current artifacts (``TaskView``).
   A check that raises is a FAIL with ``check_raised``, never a crash.
2. A parent is done when every child instance is done and its own check passes (or it has
   none). A child still to run makes the parent ``children_pending``. A blocked child blocks
   the parent and is named as the blocker (``child_blocked``), unless the parent declares
   ``on_child_blocked="partial"`` (S1): then it may finish once every child is done or
   blocked, and the blocked ones stay named in ``blockers`` for readers (ADR-P24). A
   child that can never start because an instance it needs for its key is blocked
   (unreached), or that finalization voided, counts as blocked here.
3. A keyed Task taken as a whole (what a sibling's ``needs`` waits on) is done when every
   current key's instance is done, under the same partial rule; ``keys_pending`` while its
   key source has no record yet.
4. F2 (ADR-P05, decision 34): an instance is not done while the rebuild plan lists its
   writer's version as runnable for its run (``rebuild_pending``, before its own check),
   so a finish with the old version still in place is a veto. A writer that saved a
   pending revision is done: the plan skips a version under review.
5. A failing check on a finish request is a veto: its message is the next turn's runtime
   message. The ``max_finish_vetoes``-th failing finish since the instance was last entered
   (or reset) is FAIL instead, and the instance becomes ``blocked``. ``check_raised`` and
   ``child_blocked`` are FAIL at once: another turn cannot clear them.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from aksha.runtime.budgets.limits import vetoes_since_entry
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.checks import check_name, run_check
from aksha.runtime.tasks.rebuilds import rebuild_targets
from aksha.runtime.tasks.status import CHECK_EVALUATED, CHECK_RESET, RunState, run_state
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import (
    Evaluation,
    EvaluationCode,
    FinishVerdict,
    InstanceRef,
    ResetCause,
)
from aksha.runtime.tasks.view import TaskView

FIRST_KEYS = 5
"""How many failing keys an evaluation names (the rest are counted)."""

EvaluatedOn = Literal["entry", "finish", "parent"]


def evaluate_completion(view: LogView, tree: CompiledTree, ref: InstanceRef) -> Evaluation:
    """Evaluate ``ref``'s completion on ``view`` (module doc, rules 1 and 2). Pure."""
    return evaluate_in(run_state(view, tree, ref.run), ref)


def evaluate_in(state: RunState, ref: InstanceRef) -> Evaluation:
    """:func:`evaluate_completion` over an indexed run state."""
    node = state.tree.node(ref.task_path)
    task = node.task
    if node.children:
        children = [ref.child(c.id) for c in task.children]
        pending = [c.instance for c in children if not state.settled(c)]
        if pending:
            return Evaluation(
                ref.instance,
                ok=False,
                message=f"waiting on {', '.join(pending[:FIRST_KEYS])}",
                count=len(pending),
                keys=tuple(pending[:FIRST_KEYS]),
                code="children_pending",
            )
        blocked = blockers(state, children)
        if blocked and task.on_child_blocked == "block":
            return Evaluation(
                ref.instance,
                ok=False,
                message=f"blocked by {', '.join(blocked[:FIRST_KEYS])}",
                count=len(blocked),
                keys=blocked[:FIRST_KEYS],
                code="child_blocked",
                blockers=blocked,
            )
        if task.done is None:
            return Evaluation(ref.instance, ok=True, blockers=blocked)
        own = _own(state, ref)
        return Evaluation(
            own.instance, own.ok, own.message, own.count, own.keys, own.clears,
            own.code, blocked, own.check,
        )  # fmt: skip
    return _own(state, ref)


def _own(state: RunState, ref: InstanceRef) -> Evaluation:
    if ref in rebuild_targets(state.view, state.tree, ref.run):
        return Evaluation(
            ref.instance, ok=False, code="rebuild_pending",
            message="an input of your saved version changed: save it again from the "
            "current inputs",
        )  # fmt: skip
    check = state.tree.node(ref.task_path).task.done
    if check is None:
        return Evaluation(ref.instance, ok=True)
    result, raised = run_check(check, TaskView(state.view, state.tree, ref))
    code: EvaluationCode = "" if result.ok else ("check_raised" if raised else "check_failed")
    return Evaluation(
        ref.instance,
        ok=result.ok,
        message=result.message,
        count=result.count,
        keys=tuple(result.keys[:FIRST_KEYS]),
        clears=result.clears,
        code=code,
        check=check_name(check),
    )


def task_done(state: RunState, path: str) -> Evaluation:
    """A Task taken over all its keys (rule 3): what a sibling's ``needs`` waits on."""
    task = state.tree.node(path).task
    if state.keys(path) is None:
        return Evaluation(path, ok=False, message="no keys yet", code="keys_pending")
    refs = [r for r in state.refs(path) if state.status(r) != "withdrawn"]
    pending = [r.instance for r in refs if not state.settled(r)]
    if pending:
        return Evaluation(
            path, ok=False, message=f"waiting on {', '.join(pending[:FIRST_KEYS])}",
            count=len(pending), keys=tuple(pending[:FIRST_KEYS]), code="children_pending",
        )  # fmt: skip
    blocked = blockers(state, refs)
    if blocked and task.on_child_blocked == "block":
        return Evaluation(
            path, ok=False, message=f"blocked by {', '.join(blocked[:FIRST_KEYS])}",
            count=len(blocked), keys=blocked[:FIRST_KEYS], code="child_blocked",
            blockers=blocked,
        )  # fmt: skip
    return Evaluation(path, ok=True, blockers=blocked)


def finish_verdict(view: LogView, tree: CompiledTree, ref: InstanceRef) -> FinishVerdict:
    """What a finish request of ``ref`` becomes now (module doc, rule 4). Pure."""
    return verdict_for(view, tree, ref, evaluate_completion(view, tree, ref))


def verdict_for(
    view: LogView, tree: CompiledTree, ref: InstanceRef, evaluation: Evaluation
) -> FinishVerdict:
    """The verdict for ``evaluation`` of ``ref``, counting the vetoes already recorded."""
    if evaluation.ok:
        return "done"
    if evaluation.code in ("check_raised", "child_blocked"):
        return "fail"
    limit = tree.node(ref.task_path).task.max_finish_vetoes
    used = vetoes_since_entry(view, ref.run, ref.instance)
    return "fail" if used + 1 >= limit else "veto"


def evaluated_fact(ref: InstanceRef, evaluation: Evaluation, *, on: EvaluatedOn) -> NewEvent:
    """The ``completion_check.evaluated`` fact for one evaluation (U3)."""
    payload: dict[str, object] = {
        "task": ref.task_path,
        "key": ref.key,
        "on": on,
        "ok": evaluation.ok,
        "message": evaluation.message,
        "count": evaluation.count,
        "keys": list(evaluation.keys),
        "code": evaluation.code,
        "blockers": list(evaluation.blockers),
    }
    if evaluation.check:
        payload["check"] = evaluation.check
    if evaluation.clears is not None:
        payload["clears"] = evaluation.clears
    return NewEvent(CHECK_EVALUATED, payload, run=ref.run, instance=ref.instance)


def reset_fact(ref: InstanceRef, cause: ResetCause) -> NewEvent:
    """The ``completion_check.reset`` fact: a blocked instance may run again (U3 or U4)."""
    payload = {"task": ref.task_path, "key": ref.key, "cause": cause}
    return NewEvent(CHECK_RESET, payload, run=ref.run, instance=ref.instance)


def blockers(state: RunState, refs: Iterable[InstanceRef]) -> tuple[str, ...]:
    """The instances among ``refs`` that settled without being done, in order.

    Blocked, unreached (an instance they need for their key is blocked) or voided by
    finalization (ADR-P24): each is a key whose output a reader sees as an unknown.
    """
    return tuple(
        r.instance
        for r in refs
        if state.status(r) == "blocked" or state.unreached(r) is not None or state.voided(r)
    )
