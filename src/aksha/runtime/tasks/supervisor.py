"""The supervisor Task (ADR-P15 D7): a parent Task that takes turns of its own to steer.

A supervisor is a parent Task that declares ``wakes_on`` (event types that give it a turn)
or a ``brief`` (its own prompt). It is built from the same parts as any Task; there is no
spawn tool. Its steering is data it saves (directives, priorities, a plan whose keys a
key source reads), which the scheduler then acts on.

On an entry or "next" candidate the rule, from the super-agent design (section 7.1), is:

1. take a turn if a declared wake event happened in the run since its last turn;
2. else push its first runnable child;
3. else it is done if its completion passes;
4. else take a turn if a failing check is tagged ``clears="supervisor"``;
5. else every failing check waits on a worker or a person: it holds, passing the
   decision up, so the run holds.

Two limits counted from the event store bound it (section 7.2), checked before a turn:
the activation limit (its turns in this run) and the no-progress limit (its turns since a
child instance last finished or saved an artifact). Either one biting is ``fail``.
"""

from __future__ import annotations

from collections.abc import Sequence

from aksha.runtime.budgets.records import charged_turn
from aksha.runtime.tasks.completion import evaluate_in
from aksha.runtime.tasks.status import STOP_DECIDED, TASK_ENTERED, RunState
from aksha.runtime.tasks.types import InstanceRef, SupervisorMove, Task

ACTIVATION_LIMIT = 20
"""The most turns a supervisor instance takes in one run."""

NO_PROGRESS_LIMIT = 3
"""The most supervisor turns in a row with no child progress."""


def is_supervisor(task: Task) -> bool:
    """A parent Task that takes turns of its own: it declares ``wakes_on`` or a ``brief``."""
    return bool(task.children) and (bool(task.wakes_on) or task.brief is not None)


def _turn_seqs(state: RunState, ref: InstanceRef) -> list[int]:
    return [
        e.seq
        for e in state.view.events
        if e.run == ref.run and e.instance == ref.instance and charged_turn(e) is not None
    ]


def _progress_seq(state: RunState, ref: InstanceRef) -> int:
    below = f"{ref.instance}/"
    last = 0
    for e in state.view.events:
        if e.run != ref.run or not (e.instance or "").startswith(below):
            continue
        if e.type == "artifact.saved" or (e.type == STOP_DECIDED and e.payload.get("pop")):
            last = e.seq
    return last


def _entered_seq(state: RunState, ref: InstanceRef) -> int:
    return max(
        (
            e.seq
            for e in state.view.events
            if e.type == TASK_ENTERED and e.run == ref.run and e.instance == ref.instance
        ),
        default=0,
    )


def supervisor_step(
    state: RunState,
    ref: InstanceRef,
    runnable_children: Sequence[InstanceRef],
    *,
    activation_limit: int = ACTIVATION_LIMIT,
    no_progress_limit: int = NO_PROGRESS_LIMIT,
) -> SupervisorMove:
    """The rule of the module doc for supervisor instance ``ref``. Pure.

    ``runnable_children`` is what the scheduler found runnable below ``ref``, in pick order.
    """
    task = state.tree.node(ref.task_path).task
    turns = _turn_seqs(state, ref)
    since = max(turns, default=_entered_seq(state, ref))
    wake = any(
        e.type in task.wakes_on and e.run == ref.run and e.seq > since for e in state.view.events
    )

    def turn(reason: str) -> SupervisorMove:
        if len(turns) >= activation_limit:
            return SupervisorMove("fail", "activation_limit")
        progress = _progress_seq(state, ref)
        if sum(1 for s in turns if s > progress) >= no_progress_limit:
            return SupervisorMove("fail", "no_progress_limit")
        return SupervisorMove("turn", reason)

    if wake:
        return turn("wake")
    if runnable_children:
        return SupervisorMove("push", "child", runnable_children[0])
    evaluation = evaluate_in(state, ref)
    if evaluation.ok:
        return SupervisorMove("done")
    if evaluation.clears == "supervisor":
        return turn("supervisor")
    return SupervisorMove("hold", evaluation.clears or evaluation.code or "person")
