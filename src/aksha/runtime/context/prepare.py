"""One turn's context, end to end: assemble, summarise when due, assemble again (ADR-P08 D7).

:func:`prepare_context` is what the agent loop calls before a model step:

1. It assembles the context from the log as it stood before this turn's summary step
   started (or now, if none did), so the decision and the summariser's request are the
   same on a restart.
2. When :func:`summary.summary_through` says a summary is due, it runs the summary step
   ``<turn>/compact`` on the durable step executor; a recorded step returns its summary
   and calls no model. One summary step per turn at most.
3. It reads the log again and assembles the turn's context, which now stands the summary
   in for the older turns. Over the limit it returns :class:`ContextOverLimit`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.types import ArtifactKind
from aksha.runtime.context.assembly import GENERATE, assemble_context
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
from aksha.runtime.execution.types import StepId
from aksha.runtime.models.routing import AliasTable
from aksha.runtime.projections.view import load_view
from aksha.runtime.stop_policy.types import Prep
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import InstanceRef
from aksha.runtime.tools.types import ToolView


@dataclass(frozen=True)
class Prepared:
    """A turn's context, or why nothing may be sent, and the summary step if one ran."""

    context: Assembled | ContextOverLimit
    summary: SummaryOutcome | None = None


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
    turns = instance_turns(before, instance.run, instance.instance)
    through = summary_through(before, step, turns, plan, policy)
    if through is None:
        return Prepared(build())
    previous = latest_compaction(before, instance.run, instance.instance)
    outcome = summarise(executor, table, step, turns, through, policy, previous)
    return Prepared(build(), outcome)
