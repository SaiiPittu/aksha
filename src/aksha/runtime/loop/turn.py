"""``run_turn``: one turn of one Task instance (ADR-P06 D1 to D3, D5 to D7).

A turn, in order, each part read from projections of the event store and every write
made through the durable step executor:

0. On a Task whose declared choice saves an artifact, the runtime's save from the
   person's answer, when one is due (``loop/answers.py``), recorded as its own step. A
   refused save ends the turn as a ``fault`` naming the refusal, so the instance ends
   blocked with the code and message instead of retrying a save nothing can fix.
1. The stop-policy stack's preparation for the instance (``prep_for``), read from the log
   as it stood before the turn's first step, so a resumed turn prepares the same request:
   brief lines for the context, and a tool narrowing the registry and the gate apply.
   ``save_artifact`` is described with the kinds the Task writes and their schemas.
2. The context (ADR-P08 ``prepare_context``): masking, and a recorded summary step when
   one is due and stop-policy evaluation's ``summary_refusal`` approves it (D7). Over the
   hard limit nothing is sent and the turn ends ``context_overflow``.
3. The model step ``tNN`` through the Task's generation alias, its ``args_hash`` the
   context hash (ADR-P07): a resumed turn returns the recorded reply, and a rebuilt
   context that hashes differently raises ``StepMismatch``, which ends the turn ``fault``
   with ``step_mismatch`` (the run's fault, never a silently different request). Its
   policy (``model_policy``) is the agent's ``Limits``: the timeout, the retries and the
   backoff, with its waiting bounded by what is left of the run's active time (P14 B2).
4. Each call position, in listed order (``calls.run_calls``), unless the reply is
   malformed, which runs none of its calls.
5. The turn end, rebuilt from the recorded step events by ``recorded_turn_end``: the
   loop keeps nothing between turns and counts nothing (P-2).
"""

from __future__ import annotations

from dataclasses import replace

from aksha.runtime.budgets.meter import meter
from aksha.runtime.context.assembly import GENERATE
from aksha.runtime.context.prepare import prepare_context
from aksha.runtime.context.turns import as_of
from aksha.runtime.context.types import ContextOverLimit
from aksha.runtime.execution.errors import StepMismatch
from aksha.runtime.execution.ports import DurableStepExecutor
from aksha.runtime.execution.types import StepId, StepPolicy
from aksha.runtime.loop.answers import save_from_answer
from aksha.runtime.loop.calls import run_calls
from aksha.runtime.loop.classify import is_malformed, overflow_end, recorded_turn_end
from aksha.runtime.loop.types import STEP_MISMATCH, LoopSetup, TurnEnd
from aksha.runtime.models.steps import MODEL_STEP_POLICY, generation_step
from aksha.runtime.projections.view import LogView
from aksha.runtime.stop_policy.evaluation import summary_refusal
from aksha.runtime.stop_policy.stack import narrowing_of, prep_for
from aksha.runtime.stop_policy.types import StopView
from aksha.runtime.tasks.choices import answer_kinds
from aksha.runtime.tasks.types import InstanceRef
from aksha.runtime.tools.loader_added import offered_for_task


class TurnUnfinished(RuntimeError):
    """A turn returned with a step the log does not record: a bug in the loop, never data."""


def stop_view(setup: LoopSetup, run: str, log: LogView | None = None) -> StopView:
    """The stop policies' view of ``log`` (the owner's log as it stands now by default)."""
    return StopView(
        setup.view() if log is None else log, setup.tree, run, setup.limits,
        agent_policies=setup.agent.stop_policies, context_policy=setup.policy(),
        blobs=setup.blobs,
    )  # fmt: skip


def model_policy(setup: LoopSetup, view: LogView, run: str) -> StepPolicy:
    """The model step's policy: the agent's timeout, retries and backoff (module doc, 3).

    Waiting is bounded by the run's active time left in ``view`` (None when the run's
    budget does not cap it), so a long provider outage ends the step, not the budget.
    """
    limits = setup.limits
    return replace(
        MODEL_STEP_POLICY,
        timeout_ms=limits.model_timeout_ms,
        max_retries=limits.max_retries,
        backoff=limits.backoff,
        jitter=limits.jitter,
        jitter_source=limits.jitter_source,
        wait_cap_ms=meter(view, run).remaining("active_ms"),
    )


def run_turn(
    setup: LoopSetup, executor: DurableStepExecutor, ref: InstanceRef, turn: int
) -> TurnEnd:
    """Run (or resume) turn ``turn`` of ``ref`` and return its end (module doc)."""
    refused = save_from_answer(setup, executor, ref, turn)  # a person's choice, saved by code
    if refused is not None:  # nothing the model does fixes it: the instance ends, saying why
        return TurnEnd(ref.run, ref.instance, turn, "fault", refused, setup.view().seq_read)
    step = StepId(ref.run, ref.instance, turn=turn)
    before = as_of(as_of(setup.view(), step), step.compaction())
    prep = prep_for(stop_view(setup, ref.run, before), ref)
    task = setup.task(ref)
    kinds = setup.artifacts.kinds if setup.artifacts is not None else setup.kinds
    writable = {
        n: kinds[n]
        for n in (setup.tree.kind(w) for w in task.writes)
        if n in kinds and n not in answer_kinds(setup.tree)  # only the runtime saves these
    }
    tools = offered_for_task(setup.registry.offered(task, prep.tools), writable)
    step_policy = model_policy(setup, before, ref.run)
    try:
        prepared = prepare_context(
            setup.store, setup.owner, executor, setup.table, setup.tree, ref, turn=turn,
            inputs=setup.inputs(ref), prep=replace(prep, tools=None), policy=setup.policy(),
            tools=tools, kinds=setup.kinds, alias=GENERATE, blobs=setup.blobs,
            approve_summary=lambda log: summary_refusal(stop_view(setup, ref.run, log), ref),
            model_policy=step_policy,
        )  # fmt: skip
        context = prepared.context
        if isinstance(context, ContextOverLimit):
            return overflow_end(ref, turn, context)
        facts = context.facts
        out = generation_step(
            executor, step, setup.table, context.request,
            started=context.started_fields(), facts=lambda _: facts, policy=step_policy,
        )  # fmt: skip
        if out.result is not None and not is_malformed(out.result):
            run_calls(setup, executor, ref, turn, out.result.tool_calls, narrowing_of(prep))
    except StepMismatch:
        return TurnEnd(ref.run, ref.instance, turn, "fault", STEP_MISMATCH, setup.view().seq_read)
    end = recorded_turn_end(setup.view(), ref, turn, blobs=setup.blobs)
    if end is None:
        raise TurnUnfinished(f"{step} returned with a call position that has no recorded end")
    return end
