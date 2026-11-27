"""One turn's context, end to end: assemble, summarise when due, assemble again (ADR-P08 D7).

:func:`prepare_context` is what the agent loop calls before a model step:

1. It assembles the context from the log as it stood before this turn's summary step
   started (or now, if none did), so the decision and the summariser's request are the
   same on a restart.
2. When :func:`summary.summary_through` says a summary is due, the Task and run limits
   approve it through stop-policy evaluation (``approve_summary``, D7: the loop passes
   ``stop_policy.evaluation.summary_refusal``), judged on the same log, so a restart
   decides the same; a refusal runs no model call. An approved summary runs as the step
   ``<turn>/compact`` on the durable step executor; a recorded step returns its summary
   and calls no model. One summary step per turn at most.
3. It reads the log again and assembles the turn's context, which now stands the summary
   in for the older turns. Over the limit it returns :class:`ContextOverLimit`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.types import ArtifactKind
from aksha.runtime.context.assembly import GENERATE, assemble_context
from aksha.runtime.context.reentry import recorded_choice
from aksha.runtime.context.summary import (
    SummaryOutcome,
    latest_compaction,
    summarise,
    summary_through,
)
from aksha.runtime.context.turns import as_of, instance_turns
from aksha.runtime.context.types import (
    Assembled,
    AssemblyInputs,
    ContextOverLimit,
    ContextPolicy,
)
from aksha.runtime.execution.ports import DurableStepExecutor
from aksha.runtime.execution.types import StepId, StepPolicy
from aksha.runtime.models.routing import AliasTable
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.stop_policy.types import Prep
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import InstanceRef
from aksha.runtime.tools.types import ToolView

SummaryApproval = Callable[[LogView], str | None]
"""The stop-policy approval of a summary step (D7): a refusal code, or None to approve."""


@dataclass(frozen=True)
class Prepared:
    """A turn's context, or why nothing may be sent, and the summary step if one ran.

    ``summary_refused`` is the approval's refusal code when a summary was due and the
    limits refused it (no summary step ran).
    """

    context: Assembled | ContextOverLimit
    summary: SummaryOutcome | None = None
    summary_refused: str | None = None


def prepare_context(
    store: EventStore,
    owner: str,
    executor: DurableStepExecutor,
    table: AliasTable,
    tree: CompiledTree,
    instance: InstanceRef,
    *,
    turn: int,
    inputs: AssemblyInputs,
    prep: Prep,
    policy: ContextPolicy,
    tools: tuple[ToolView, ...] = (),
    kinds: Mapping[str, ArtifactKind] | None = None,
    alias: str = GENERATE,
    recovery: bool = False,
    blobs: BlobStore | None = None,
    approve_summary: SummaryApproval | None = None,
    model_policy: StepPolicy | None = None,
) -> Prepared:
    """Turn ``turn``'s context for ``instance``, with a summary step when due (module doc)."""
    step = StepId(instance.run, instance.instance, turn=turn)

    def build(view_seq: int | None = None) -> Assembled | ContextOverLimit:
        view = load_view(store, owner)
        if view_seq is not None:
            view = view.upto(view_seq)
        return assemble_context(
            view, tree, instance, turn=turn, inputs=inputs, prep=prep, policy=policy,
            tools=tools, kinds=kinds, alias=alias, recovery=recovery, blobs=blobs,
        )  # fmt: skip

    if policy.summary is None:
        return Prepared(build())
    before = as_of(as_of(load_view(store, owner), step), step.compaction())
    first = build(before.seq_read)
    plan = first.plan
    if plan is None:
        return Prepared(first)
    choice = recorded_choice(before, instance)
    start = 1 if choice is None else choice.from_turn
    turns = tuple(
        t
        for t in instance_turns(before, instance.run, instance.instance, blobs=blobs)
        if t.number >= start
    )  # a summary covers the entry's own turns only (decision 36)
    through = summary_through(before, step, turns, plan, policy)
    if through is None:
        return Prepared(build())
    refused = None if approve_summary is None else approve_summary(before)
    if refused is not None:
        return Prepared(build(), None, refused)
    previous = latest_compaction(before, instance.run, instance.instance, since_turn=start)
    outcome = summarise(
        executor, table, step, turns, through, policy, previous, model_policy=model_policy
    )
    return Prepared(build(), outcome)
