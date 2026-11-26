"""The ``steer`` command's instance check (ADR-P27 B2): only an instance that reads input."""

from __future__ import annotations

from aksha.runtime.inputs.commands import steer_spec
from aksha.runtime.session.types import Command, CommandAccepted, CommandRefused
from aksha.runtime.stop_policy.types import StopCandidate
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import Task
from tests.support.budgets import RUN
from tests.support.stop import decide
from tests.support.tasks import TaskLog, has


class _Ids:
    def next(self, owner: str, prefix: str) -> str:
        return f"{prefix}-0001"


def _decide(log: TaskLog, tree: object, instance: str) -> CommandAccepted | CommandRefused:
    args = {"run": RUN, "text": "use the helper", "lane": "follow_up", "by": "person",
            "instance": instance}  # fmt: skip
    spec = steer_spec({"a": tree}, _Ids())  # type: ignore[dict-item]
    return spec.decide(Command("steer", log.owner, "S-0001", args, "c-1"), log.view())


def test_a_parent_that_takes_no_turns_cannot_be_steered() -> None:
    """A plain parent never calls the model or proposes a finish: the input would be lost."""
    edit = Task("edit", writes=("x",), done=has("x"))
    tree = compile_tree("a", (Task("work", children=(edit,)),))
    log = TaskLog()
    log.started()
    decide(log, tree, StopCandidate(RUN, "next"))
    refused = _decide(log, tree, "work")
    assert isinstance(refused, CommandRefused) and refused.code == "instance_not_open"
    assert isinstance(_decide(log, tree, "work/edit"), CommandAccepted)


def test_a_supervisor_can_be_steered() -> None:
    edit = Task("edit", writes=("x",), done=has("x"))
    tree = compile_tree("a", (Task("lead", children=(edit,), brief="Steer the work."),))
    log = TaskLog()
    log.started()
    decide(log, tree, StopCandidate(RUN, "next"))
    assert isinstance(_decide(log, tree, "lead"), CommandAccepted)
