"""``run_turn``: one turn of one Task instance (ADR-P06 D1 to D3, D5 to D7).

A turn, in order, each part read from projections of the event store and every write
made through the durable step executor:

1. The stop-policy stack's preparation for the instance (``prep_for``), read from the log
   as it stood before the turn's first step, so a resumed turn prepares the same request:
   brief lines for the context, and a tool narrowing the registry and the gate apply.
2. The context (ADR-P08 ``prepare_context``): masking, and a recorded summary step when
   one is due and stop-policy evaluation's ``summary_refusal`` approves it (D7). Over the
   hard limit nothing is sent and the turn ends ``context_overflow``.
3. The model step ``tNN`` through the Task's generation alias, its ``args_hash`` the
   context hash (ADR-P07): a resumed turn returns the recorded reply, and a rebuilt
   context that hashes differently raises ``StepMismatch``, which ends the turn ``fault``
   with ``step_mismatch`` (the run's fault, never a silently different request).
4. Each call position, in listed order (``calls.run_calls``), unless the reply is
   malformed, which runs none of its calls.
5. The turn end, rebuilt from the recorded step events by ``recorded_turn_end``: the
   loop keeps nothing between turns and counts nothing (P-2).
"""

from __future__ import annotations

from dataclasses import replace

from aksha.runtime.context.assembly import GENERATE
from aksha.runtime.context.prepare import prepare_context
from aksha.runtime.context.turns import as_of
from aksha.runtime.context.types import ContextOverLimit
from aksha.runtime.execution.errors import StepMismatch
from aksha.runtime.execution.ports import DurableStepExecutor
from aksha.runtime.execution.types import StepId
from aksha.runtime.loop.calls import run_calls
from aksha.runtime.loop.classify import is_malformed, overflow_end, recorded_turn_end
from aksha.runtime.loop.types import STEP_MISMATCH, LoopSetup, TurnEnd
from aksha.runtime.models.steps import generation_step
from aksha.runtime.projections.view import LogView
from aksha.runtime.stop_policy.evaluation import summary_refusal
from aksha.runtime.stop_policy.stack import narrowing_of, prep_for
from aksha.runtime.stop_policy.types import StopView
from aksha.runtime.tasks.types import InstanceRef


class TurnUnfinished(RuntimeError):
    """A turn returned with a step the log does not record: a bug in the loop, never data."""


def stop_view(setup: LoopSetup, run: str, log: LogView | None = None) -> StopView:
    """The stop policies' view of ``log`` (the owner's log as it stands now by default)."""
    return StopView(
        setup.view() if log is None else log, setup.tree, run, setup.limits,
        agent_policies=setup.agent.stop_policies,
    )  # fmt: skip


def run_turn(
    setup: LoopSetup, executor: DurableStepExecutor, ref: InstanceRef, turn: int
) -> TurnEnd:
    """Run (or resume) turn ``turn`` of ``ref`` and return its end (module doc)."""
    step = StepId(ref.run, ref.instance, turn=turn)
    before = as_of(as_of(setup.view(), step), step.compaction())
    prep = prep_for(stop_view(setup, ref.run, before), ref)
    tools = setup.registry.offered(setup.task(ref), prep.tools)
    try:
        prepared = prepare_context(
            setup.store, setup.owner, executor, setup.table, setup.tree, ref, turn=turn,
            inputs=setup.inputs(ref), prep=replace(prep, tools=None), policy=setup.policy(),
            tools=tools, kinds=setup.kinds, alias=GENERATE, blobs=setup.blobs,
            approve_summary=lambda log: summary_refusal(stop_view(setup, ref.run, log), ref),
        )  # fmt: skip
        context = prepared.context
        if isinstance(context, ContextOverLimit):
            return overflow_end(ref, turn, context)
        facts = context.facts
        out = generation_step(
            executor, step, setup.table, context.request,
            started=context.started_fields(), facts=lambda _: facts,
        )  # fmt: skip
        if out.result is not None and not is_malformed(out.result):
            run_calls(setup, executor, ref, turn, out.result.tool_calls, narrowing_of(prep))
    except StepMismatch:
        return TurnEnd(ref.run, ref.instance, turn, "fault", STEP_MISMATCH, setup.view().seq_read)
    end = recorded_turn_end(setup.view(), ref, turn)
    if end is None:
        raise TurnUnfinished(f"{step} returned with a call position that has no recorded end")
    return end
