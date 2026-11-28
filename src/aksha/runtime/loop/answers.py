"""The runtime's save from a person's answer (ADR-P04 choice binding, ADR-P17 D2).

A Task whose declared choice ``saves`` an artifact never lets the model write it: a tool's
save of a kind a declared choice saves is refused ``answer_only`` (``calls._dispatched``).
Instead, at the start of each turn of such an instance, before its context is prepared,
:func:`save_from_answer` asks ``tasks.choices.answer_save_plan`` whether there is
something to save (a fresh answer, a choice to copy in a rebuild, or an empty choice with
nothing to ask) and, if so, saves it as the reserved step ``<run>/<instance>/tNN/c01/derived``:

- through the one save path the agent loop's saves use (``calls._dispatched``), so the
  kind's schema and ``compute``, the recorded reads (the Task's declared reads at their
  current versions and the person's answer as a statement), one writer per slot, the
  revision's diff review and the step's confirmations all apply;
- with the plan's origin (``choice``, ``copy_choice`` or ``task``) and the answer it is
  from on the version (``artifact.saved{answer}``);
- recorded through the durable executor, so a resumed turn returns the recorded save and
  never saves twice. The step counts no tool call.

The model's turn then only finishes; the completion check (``chosen_by_person``) compares
the saved codes with the answer.
"""

from __future__ import annotations

from aksha.runtime.artifacts.types import SaveRequest
from aksha.runtime.context.notices import answer_reads
from aksha.runtime.context.reads import declared_versions
from aksha.runtime.execution.ports import DurableStepExecutor
from aksha.runtime.execution.types import Deadline, StepId, StepOutput
from aksha.runtime.loop.calls import _dispatched, tool_policy
from aksha.runtime.loop.types import LoopSetup
from aksha.runtime.policy.events import args_hash
from aksha.runtime.tasks.choices import answer_content, answer_save_plan
from aksha.runtime.tasks.types import InstanceRef
from aksha.runtime.tasks.view import TaskView
from aksha.runtime.tools.loader_added import SAVE_TOOL
from aksha.runtime.tools.types import Ok, ToolOutcome

ANSWER_SUB = "derived"
"""The sub-step of the turn's first call position the answer save is recorded under."""


def answer_step(ref: InstanceRef, turn: int) -> StepId:
    """The reserved step of turn ``turn``'s answer save: ``.../tNN/c01/derived``."""
    return StepId(ref.run, ref.instance, turn=turn, call=1, sub=ANSWER_SUB)


def save_from_answer(
    setup: LoopSetup, executor: DurableStepExecutor, ref: InstanceRef, turn: int
) -> None:
    """Save the instance's choice from the person's answer, if one is due (module doc)."""
    task = setup.task(ref)
    if task.interrupt is None or task.interrupt.saves is None:
        return
    step = answer_step(ref, turn)
    if executor.recorded(step) is not None:
        return
    view = setup.view()
    tv = TaskView(view, setup.tree, ref)
    plan = answer_save_plan(tv)
    if plan is None:
        return
    content = answer_content(tv, plan)
    reads = (
        *(v.ref.as_input() for v in declared_versions(view, setup.tree, ref, blobs=setup.blobs)),
        *answer_reads(view, ref),
    )
    request = SaveRequest(plan.kind, plan.key, content, answer=plan.answer)
    data = {"kind": plan.kind, "chosen": list(plan.chosen), "answer": plan.answer or None,
            "origin": plan.origin}  # fmt: skip
    outcome = ToolOutcome(Ok(data, reads=reads), saves=(request,))
    spec = setup.registry.get(SAVE_TOOL).spec

    def work(_: Deadline) -> StepOutput:
        return _dispatched(setup, ref, step, spec, outcome, origin=plan.origin)

    extra = {"route": "answer", "counted": False}
    executor.step(step, "tool", SAVE_TOOL, args_hash(data), work, tool_policy(spec), extra=extra)
