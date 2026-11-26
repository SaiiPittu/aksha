"""Context assembly (ADR-P08 D1 to D6): each model request as a pure projection of the log.

:func:`assemble_context` builds turn ``turn``'s request for one Task instance from the
event store's view, the compiled Task tree, the stop-policy preparation, the context
policy and the definition's text. It reads the view as it stood before that turn's model
step started (``turns.as_of``), so a restart rebuilds the same bytes and the context hash
recorded on ``step.started`` matches; the store is never rewritten, only the request.

The order keeps the stable prefix first, so a provider's prefix cache stays warm:

1. ``system``: the agent's instructions; each stop-policy brief, outer to inner; the
   fixed data rule.
2. ``user``: the read block (declared reads, rendered or by ref); the rebuild brief of a
   rebuild run; the request with the Task brief and the person's answers (never reduced);
   the recorded summary that stands for older turns.
3. The instance's own turns: each reply, then each tool result, masked by the plan.
4. ``user``: the change notice (decision 32), the recitation (only once something is
   masked) and the reason the runtime refused the last finish.
5. The tools the stop policies left (``Prep.tools``), and the ``tool_choice`` hint.

Over ``context_limit_chars`` nothing is sent: :class:`ContextOverLimit` is returned, and
the loop ends the turn ``context_overflow`` for stop-policy evaluation to judge. Each
instance's context is built from its own events only (decision 8): another instance's
turns, and another agent's, never enter it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from aksha.runtime.artifacts.lineage import reads_to_record
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.types import ArtifactKind, InputRef
from aksha.runtime.context.masking import arguments_stub, plan_masking, render_stub, stale_reads
from aksha.runtime.context.notices import (
    answers,
    change_notice,
    continue_reason,
    rebuild_brief_for,
    rebuild_notice,
    recite,
)
from aksha.runtime.context.reads import RenderedReads, render_reads
from aksha.runtime.context.summary import latest_compaction, summary_message
from aksha.runtime.context.turns import as_of, instance_turns
from aksha.runtime.context.types import (
    DATA_RULE,
    Assembled,
    AssemblyInputs,
    Compaction,
    ContextOverLimit,
    ContextPolicy,
    Masked,
    StaleRead,
    Turn,
)
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.types import StepId
from aksha.runtime.grounding.spans import span_index, spans_shown
from aksha.runtime.invalidation.view import ReadChecker
from aksha.runtime.models.types import GenerationRequest, Message, ToolCall
from aksha.runtime.projections.view import LogView
from aksha.runtime.stop_policy.types import Prep
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import InstanceRef
from aksha.runtime.tools.types import ToolView

GENERATE = "generate"
"""The implicit generation alias every Task has."""


def measure(request: GenerationRequest) -> int:
    """The request's size: characters of its canonical JSON, the hash fields excluded."""
    return request.context_chars


@dataclass(frozen=True)
class _Parts:
    """Everything of one context that masking does not change."""

    head: tuple[Message, ...]
    turns: tuple[Turn, ...]
    compaction: Compaction | None
    reads: RenderedReads
    tail: tuple[Message, ...]
    tools: tuple[ToolView, ...]
    tool_choice: str | None


def _head(
    view: LogView,
    instance: InstanceRef,
    inputs: AssemblyInputs,
    prep: Prep,
    policy: ContextPolicy,
    reads: RenderedReads,
    blobs: BlobStore | None,
) -> tuple[Message, ...]:
    out: list[Message] = []
    if inputs.instructions:
        out.append(Message("system", inputs.instructions))
    out += [Message("system", b) for b in prep.brief if b]
    out.append(Message("system", DATA_RULE))
    if reads.text:
        out.append(Message("user", reads.text))
    brief = rebuild_brief_for(view, instance, blobs=blobs)
    if brief is not None:
        out.append(Message("user", rebuild_notice(brief, policy)))
    request = "\n\n".join(
        p for p in (inputs.request, inputs.brief and f"Task: {inputs.brief}") if p
    )
    if request:
        out.append(Message("user", request))
    said = answers(view, instance)
    if said:
        out.append(Message("user", said))
    return tuple(out)


def _turn_messages(turns: tuple[Turn, ...], masks: Mapping[str, Masked]) -> list[Message]:
    out: list[Message] = []
    for t in turns:
        calls = []
        for call, pair in zip(t.calls, t.pairs, strict=True):
            m = masks.get(pair.step)
            args = arguments_stub(m) if m is not None and m.arguments else call.arguments
            calls.append(ToolCall(call.call_id, call.name, args))
        out.append(Message("assistant", t.text, tuple(calls)))
        for pair in t.pairs:
            m = masks.get(pair.step)
            out.append(Message("tool", render_stub(m) if m else pair.text, call_id=pair.call_id))
    return out


def _stale(
    view: LogView, instance: InstanceRef, masks: tuple[Masked, ...], turns: tuple[Turn, ...],
    blobs: BlobStore | None,
) -> tuple[StaleRead, ...]:  # fmt: skip
    hidden = {m.step for m in masks if m.reason == "stale"}
    out: list[StaleRead] = []
    checker = ReadChecker(view, blobs=blobs) if hidden else None
    for t in turns:
        for p in t.pairs:
            if p.step in hidden:
                out += stale_reads(view, instance.run, instance.instance, p, checker=checker)
    return tuple(dict.fromkeys(out))


def _request(
    step: StepId,
    alias: str,
    parts: _Parts,
    masks: tuple[Masked, ...],
    view: LogView,
    instance: InstanceRef,
    inputs: AssemblyInputs,
    policy: ContextPolicy,
    blobs: BlobStore | None,
) -> tuple[GenerationRequest, tuple[StaleRead, ...], tuple[InputRef, ...]]:
    messages = list(parts.head)
    if parts.compaction is not None:
        messages.append(summary_message(parts.compaction))
    messages += _turn_messages(parts.turns, {m.step: m for m in masks})
    stale = _stale(view, instance, masks, parts.turns, blobs)
    notice_reads: tuple[InputRef, ...] = ()
    if stale:
        text, shown = change_notice(view, stale, policy, blobs=blobs)
        messages.append(Message("user", text))
        notice_reads = tuple(v.ref.as_input() for v in shown)
    if masks:
        messages.append(Message("user", recite(view, instance, masks, stale, brief=inputs.brief)))
    messages += parts.tail
    request = GenerationRequest(str(step), alias, tuple(messages), parts.tools, parts.tool_choice)
    return request, stale, notice_reads


def assemble_context(
    view: LogView,
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
) -> Assembled | ContextOverLimit:
    """Turn ``turn``'s request for ``instance`` (module doc). Pure: it reads only ``view``."""
    step = StepId(instance.run, instance.instance, turn=turn)
    v = as_of(view, step)
    compaction = latest_compaction(v, instance.run, instance.instance)
    covered = compaction.through_turn if compaction else 0
    turns = tuple(
        t for t in instance_turns(v, instance.run, instance.instance) if covered < t.number < turn
    )
    reads = render_reads(v, tree, instance, policy, kinds or {}, blobs=blobs)
    kept = tuple(t for t in tools if prep.tools is None or t.name in prep.tools)
    reason = continue_reason(v, instance, turn)
    parts = _Parts(
        head=_head(v, instance, inputs, prep, policy, reads, blobs),
        turns=turns,
        compaction=compaction,
        reads=reads,
        tail=(Message("user", reason),) if reason else (),
        tools=kept,
        tool_choice=prep.tool_choice,
    )

    def sized(masks: tuple[Masked, ...]) -> int:
        built, _, _ = _request(step, alias, parts, masks, v, instance, inputs, policy, blobs)
        return measure(built)

    plan = plan_masking(
        v, instance.run, instance.instance, turns, policy, sized, turn=turn, recovery=recovery,
        blobs=blobs,
    )  # fmt: skip
    request, stale, notice_reads = _request(
        step, alias, parts, plan.masked, v, instance, inputs, policy, blobs
    )
    if plan.over_limit:
        return ContextOverLimit(plan.final_chars, policy.context_limit_chars, plan)
    return Assembled(
        request=request,
        rendered=reads.rendered,
        plan=plan,
        compaction=compaction,
        stale=stale,
        facts=_facts(v, instance, step, reads, notice_reads),
    )


def _facts(
    view: LogView,
    instance: InstanceRef,
    step: StepId,
    reads: RenderedReads,
    notice_reads: tuple[InputRef, ...],
) -> tuple[NewEvent, ...]:
    """The reads and spans the context showed, to commit with the model step's result."""
    inputs = (*reads.inputs, *notice_reads)
    recorded = reads_to_record(
        view, inputs, run=instance.run, instance=instance.instance, step=str(step), via="context"
    )
    spans = sorted({s for r in reads.rendered for s in r.spans})
    shown = spans_shown(
        span_index(view), spans, run=instance.run, instance=instance.instance, step=str(step)
    )
    return (*recorded, *shown)
