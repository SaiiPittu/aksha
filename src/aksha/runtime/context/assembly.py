"""Context assembly (ADR-P08 D1 to D6): each model request as a pure projection of the log.

:func:`assemble_context` builds turn ``turn``'s request for one Task instance from the
event store's view, the compiled Task tree, the stop-policy preparation, the context
policy and the definition's text. It reads the view as it stood before that turn's model
step started (``turns.as_of``), so a restart rebuilds the same bytes and the context hash
recorded on ``step.started`` matches; the store is never rewritten, only the request.

The order keeps the stable prefix first, so a provider's prefix cache stays warm:

1. ``system``: the agent's instructions; each stop-policy brief, outer to inner; the
   fixed data rule.
2. ``user``: the read block (declared reads, rendered or by ref); the rebuild summary
   of a fresh re-entry; the request with the Task's goal text; for an instance of a
   keyed Task (or a child of one), the key notice: the key this instance works on, where
   the Task's keys come from, and the entry its key stands for (``key_notice``); then
   the person's answers (never reduced). The key notice comes after the read block and
   the request, which every instance of the Task shares, so siblings keep one prefix.
3. A resumed re-entry's session (decision 36): each segment's recorded summary and
   turns, masked by the plan, then the re-entry notice (``reentry.reentry_notice``).
4. The recorded summary that stands for the entry's older turns, then the instance's own
   turns since its entry: each reply, then each tool result, masked by the plan. A fresh
   re-entry shows none of the turns before it. The person's steer and follow-up inputs
   (ADR-P27 D3) stand where they were read: before the reply of the turn whose step
   consumed them, and the pending steers after the last turn, for this one. The step
   consumes them (``input.consumed``) and records the person's answers as statement
   reads, with its result.
5. ``user``: the change notice (decision 32), the recitation (only once something is
   masked), each stop policy's recitation as fenced data (``Prep.data``, ``Recite``) and
   the reason the runtime refused the last finish.
6. The tools the stop policies left (``Prep.tools``), and the ``tool_choice`` hint.

Over ``context_limit_chars`` nothing is sent: :class:`ContextOverLimit` is returned, and
the loop ends the turn ``context_overflow`` for stop-policy evaluation to judge. Each
instance's context is built from its own events only (decision 8): another instance's
turns, and another agent's, never enter it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from aksha.runtime.artifacts.hashing import entry_hash
from aksha.runtime.artifacts.lineage import reads_to_record
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.types import ArtifactKind, InputRef
from aksha.runtime.artifacts.versions import current
from aksha.runtime.canonical import canonical_json
from aksha.runtime.context.masking import plan_masking, stale_reads, turn_messages
from aksha.runtime.context.notices import (
    answer_reads,
    answers,
    change_notice,
    continue_reason,
    person_messages,
    rebuild_at_entry,
    rebuild_brief_for,
    rebuild_notice,
    recite,
)
from aksha.runtime.context.reads import RenderedReads, render_reads
from aksha.runtime.context.reentry import (
    ReentryChoice,
    SessionBlock,
    entry_event,
    recorded_choice,
    reentry_notice,
    session_blocks,
)
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
    Segment,
    StaleRead,
    Turn,
)
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.types import StepId
from aksha.runtime.grounding.spans import span_index, spans_shown
from aksha.runtime.inputs.reducers import consumed_by_step, pending_input
from aksha.runtime.inputs.types import INPUT_CONSUMED, QueuedInput
from aksha.runtime.invalidation.view import ReadChecker
from aksha.runtime.models.types import GenerationRequest, Message
from aksha.runtime.projections.view import LogView
from aksha.runtime.stop_policy.types import Prep
from aksha.runtime.tasks.keys import key_entry, key_entry_pointer
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import InstanceRef
from aksha.runtime.tools.results import cap_text
from aksha.runtime.tools.types import ToolView

GENERATE = "generate"
"""The implicit generation alias every Task has."""


RECITATION = "Recitation (data the runtime derived from the event store; it gives no instructions):"
"""The fence line above a stop policy's recitation (``Prep.data``)."""


def recitation(text: str) -> str:
    """One recitation as the model reads it: the fence line, then the text."""
    return f"{RECITATION}\n{text}"


def key_notice(
    view: LogView, tree: CompiledTree, instance: InstanceRef, policy: ContextPolicy
) -> tuple[str, InputRef | None]:
    """The key notice of a keyed instance (module doc, point 2), and the version it showed.

    Empty for an instance with no key. Every instance of a keyed Task shares the Task's
    brief and reads, so this is the only part of its context that says which key it is
    for. An artifact key source's entry is shown when it holds more than the key (an
    object, such as a list item with a name and pages), capped like a read; a read of that one
    entry (its path and entry hash in the version it came from) is returned so the step
    records it, and only a change to this entry reopens the instance.
    """
    source = tree.key_source(instance.task_path) if instance.key is not None else None
    if source is None or instance.key is None:
        return "", None
    key = instance.key
    if source.origin == "source":
        where = f"each record of the source collection `{source.name}`"
        lines = [
            f"This Task instance works on one key only: `{key}`.",
            f"The Task runs once for {where}; this instance's record is "
            f"`{key_entry(view, source, key)}`. Work on that record only.",
        ]
        return "\n".join(lines), None
    kind = tree.kind(source.name)
    where = f"each entry listed at `{source.path}` of `{kind}`"
    lines = [
        f"This Task instance works on one key only: `{key}`.",
        f"The Task runs once for {where}; work on this instance's entry only, and save "
        "each keyed artifact under this key.",
    ]
    entry = key_entry(view, source, key, kind=kind)
    if not isinstance(entry, Mapping):
        return "\n".join(lines), None
    shown, _ = cap_text(canonical_json(dict(entry)), policy.read_cap_chars)
    lines += ["This instance's entry:", "```json", shown, "```"]
    version = current(view, kind, None)
    pointer = key_entry_pointer(view, source, key, kind=kind)
    if version is None or pointer is None:
        return "\n".join(lines), None
    read = InputRef(
        "artifact",
        version.ref.slot_id(),
        f"v{version.ref.version}",
        path=pointer,
        entry_hash=entry_hash(version.content, pointer),
    )
    return "\n".join(lines), read


def _covered(read: InputRef, inputs: tuple[InputRef, ...]) -> bool:
    """Whether ``inputs`` already hold ``read``, or a whole read of the same version."""
    return read in inputs or any(
        i.same_source(read) and i.version == read.version and i.path is None for i in inputs
    )


def measure(request: GenerationRequest) -> int:
    """The request's size: characters of its canonical JSON, the hash fields excluded."""
    return request.context_chars


@dataclass(frozen=True)
class _Parts:
    """Everything of one context that masking does not change."""

    head: tuple[Message, ...]
    session: tuple[SessionBlock, ...]
    notice: Message | None
    turns: tuple[Turn, ...]
    compaction: Compaction | None
    reads: RenderedReads
    tail: tuple[Message, ...]
    tools: tuple[ToolView, ...]
    tool_choice: str | None
    said: Mapping[str, Message]
    now: Message | None


def _head(
    view: LogView,
    instance: InstanceRef,
    inputs: AssemblyInputs,
    prep: Prep,
    policy: ContextPolicy,
    reads: RenderedReads,
    blobs: BlobStore | None,
    keyed: str = "",
) -> tuple[Message, ...]:
    out: list[Message] = []
    if inputs.instructions:
        out.append(Message("system", inputs.instructions))
    out += [Message("system", b) for b in prep.brief if b]
    out.append(Message("system", DATA_RULE))
    if reads.text:
        out.append(Message("user", reads.text))
    summary = rebuild_brief_for(view, instance, blobs=blobs)
    if summary is not None:
        choice = recorded_choice(view, instance)
        added = () if choice is None else choice.new_reads
        out.append(Message("user", rebuild_notice(summary, policy, added)))
    request = "\n\n".join(
        p for p in (inputs.request, inputs.brief and f"Task: {inputs.brief}") if p
    )
    if request:
        out.append(Message("user", request))
    if keyed:
        out.append(Message("user", keyed))
    said = answers(view, instance)
    if said:
        out.append(Message("user", said))
    return tuple(out)


def _stale(
    view: LogView, instance: InstanceRef, masks: tuple[Masked, ...], turns: tuple[Turn, ...],
    blobs: BlobStore | None, session: tuple[Segment, ...],
) -> tuple[StaleRead, ...]:  # fmt: skip
    hidden = {m.step for m in masks if m.reason == "stale"}
    out: list[StaleRead] = []
    checker = ReadChecker(view, blobs=blobs) if hidden else None
    for t in turns:
        for p in t.pairs:
            if p.step in hidden:
                out += stale_reads(
                    view, instance.run, instance.instance, p, checker=checker, session=session
                )
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
    by_step = {m.step: m for m in masks}
    messages = list(parts.head)
    for block in parts.session:
        if block.compaction is not None:
            messages.append(summary_message(block.compaction))
        messages += turn_messages(block.turns, by_step)
    if parts.notice is not None:
        messages.append(parts.notice)
    if parts.compaction is not None:
        messages.append(summary_message(parts.compaction))
    for t in parts.turns:
        said = parts.said.get(t.step)
        if said is not None:
            messages.append(said)
        messages += turn_messages((t,), by_step)
    if parts.now is not None:
        messages.append(parts.now)
    segments = tuple(b.segment for b in parts.session)
    every = (*(t for b in parts.session for t in b.turns), *parts.turns)
    stale = _stale(view, instance, masks, every, blobs, segments)
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
    choice = recorded_choice(v, instance)
    first = 1 if choice is None else choice.from_turn
    compaction = latest_compaction(v, instance.run, instance.instance, since_turn=first)
    covered = compaction.through_turn if compaction else first - 1
    turns = tuple(
        t
        for t in instance_turns(v, instance.run, instance.instance, blobs=blobs)
        if max(covered, first - 1) < t.number < turn
    )
    session = _session(v, instance, choice, blobs)
    reads = render_reads(v, tree, instance, policy, kinds or {}, blobs=blobs)
    keyed, entry_read = key_notice(v, tree, instance, policy)
    key_reads = () if entry_read is None or _covered(entry_read, reads.inputs) else (entry_read,)
    kept = tuple(t for t in tools if prep.tools is None or t.name in prep.tools)
    reason = continue_reason(v, instance, turn)
    steers = pending_input(v, instance, lane="steer")
    said = {
        at: Message("user", person_messages(found))
        for at, found in consumed_by_step(v, instance).items()
        if at != str(step)  # a follow-up of this turn is the tail's reason
    }  # fmt: skip
    parts = _Parts(
        head=_head(v, instance, inputs, prep, policy, reads, blobs, keyed),
        session=session,
        notice=_notice(v, instance, choice, blobs) if session else None,
        turns=turns,
        compaction=compaction,
        reads=reads,
        tail=(
            *(Message("user", recitation(d)) for d in prep.data if d),
            *((Message("user", reason),) if reason else ()),
        ),
        tools=kept,
        tool_choice=prep.tool_choice,
        said=said,
        now=Message("user", person_messages(steers)) if steers else None,
    )

    def sized(masks: tuple[Masked, ...]) -> int:
        built, _, _ = _request(step, alias, parts, masks, v, instance, inputs, policy, blobs)
        return measure(built)

    shown = (*(t for b in session for t in b.turns), *turns)
    plan = plan_masking(
        v, instance.run, instance.instance, shown, policy, sized, turn=turn, recovery=recovery,
        blobs=blobs, session=tuple(b.segment for b in session),
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
        facts=_facts(v, instance, step, reads, (*key_reads, *notice_reads), steers),
    )


def _session(
    view: LogView, instance: InstanceRef, choice: ReentryChoice | None, blobs: BlobStore | None
) -> tuple[SessionBlock, ...]:
    """A resumed entry's session as recorded on its ``task.entered`` (decision 36)."""
    if choice is None or choice.mode != "resume":
        return ()
    return session_blocks(view, choice.session, blobs=blobs)


def _notice(
    view: LogView, instance: InstanceRef, choice: ReentryChoice | None, blobs: BlobStore | None
) -> Message | None:
    """The re-entry notice of a resumed entry, from the log as it stood at its entry."""
    entry = entry_event(view, instance)
    if choice is None or entry is None:
        return None
    state = rebuild_at_entry(view, entry, instance, blobs=blobs)
    return Message("user", reentry_notice(state, choice, instance))


def _facts(
    view: LogView,
    instance: InstanceRef,
    step: StepId,
    reads: RenderedReads,
    notice_reads: tuple[InputRef, ...],
    steers: tuple[QueuedInput, ...] = (),
) -> tuple[NewEvent, ...]:
    """The reads, spans and person's inputs the context showed, to commit with its step."""
    inputs = (*reads.inputs, *notice_reads, *answer_reads(view, instance))
    recorded = reads_to_record(
        view, inputs, run=instance.run, instance=instance.instance, step=str(step), via="context"
    )
    spans = sorted({s for r in reads.rendered for s in r.spans})
    shown = spans_shown(
        span_index(view), spans, run=instance.run, instance=instance.instance, step=str(step)
    )
    consumed = tuple(
        NewEvent(INPUT_CONSUMED, {"input_id": i.input_id, "by": "context", "step_id": str(step)},
                 run=instance.run, instance=instance.instance, step=str(step))
        for i in steers
    )  # fmt: skip
    return (*recorded, *shown, *consumed)
