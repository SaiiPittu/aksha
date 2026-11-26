"""Observation masking (ADR-P08 D5, S2, decision 32): which tool results a context hides.

A masked result's body is replaced by a stub naming the step, its size, what it saved
and the spans it minted; the call and its arguments stay, so a tool pair is never split.
The plan for a turn is a pure function of the log and the policy:

1. **Stale** results are hidden first and always (decision 32), in a run whose agent
   declared ``notify`` or ``rebuild`` (decision 38): a result that read an
   artifact version, a source record or a query result the invalidation view now reads
   as ``moved`` or ``gone`` for the instance's run (ADR-P05 D4: the sibling rule and
   early cutoff apply, so another run's version and an equal rebuild hide nothing),
   unless this instance (or the session it resumed, decision 36) saved the newer
   version itself. Its stub says the slot changed and that the current version is in the
   change notice at the end; the new content is never written into the old position.
2. **Recorded** masks stay: every tool step an earlier turn's model step recorded under
   ``step.started{masked}`` is hidden again, so a result once masked stays masked (the
   plan is monotone) and a restart rebuilds the same plan.
3. Above ``high_water_chars`` (or in a recovery after a provider overflow), results are
   hidden until the estimate is at or under ``low_water_chars`` (recovery: half of it):
   **superseded** results first (the artifact they saved has a newer version, the same
   call was repeated later, or this instance saved a newer version of what it read), then
   the oldest. Never one of the newest ``keep_recent_pairs`` pairs (recovery: the newest
   one) and never an exempt tool's result.
4. A superseded store call's arguments are stubbed as well (S2), naming the version it saved.

With nothing hidden the context is byte for byte the one built with masking off. A
resumed context (decision 36) plans over its session's turns and its own together: the
session's recorded masks stay hidden too, and a newer version the session saved counts
as the instance's own.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.types import ArtifactRef
from aksha.runtime.artifacts.versions import versions
from aksha.runtime.canonical import canonical_json
from aksha.runtime.context.types import (
    CONTEXT_POLICY_VERSION,
    ContextPolicy,
    Masked,
    MaskingPlan,
    MaskReason,
    Segment,
    StaleRead,
    ToolPair,
    Turn,
)
from aksha.runtime.execution.errors import StepIdInvalid
from aksha.runtime.execution.types import StepId
from aksha.runtime.invalidation.modes import masks_stale
from aksha.runtime.invalidation.view import ReadChecker
from aksha.runtime.models.types import Message, ToolCall
from aksha.runtime.projections.view import LogView

_SOURCE_KINDS = frozenset({"record", "query", "time"})
"""The non-artifact reads whose versions the invalidation view tracks (decision 36)."""

Measure = Callable[[tuple[Masked, ...]], int]
"""The size of the context with these results hidden: assembly's own measure."""


def render_stub(m: Masked) -> str:
    """The text the model sees in place of a hidden result."""
    if m.reason == "stale":
        changed = ", ".join(m.changed)
        return f"[superseded: {changed} changed, the current version is in the change notice below]"
    parts = [f"[result hidden: {m.tool} at {m.step}, {m.chars} characters"]
    if m.refs:
        parts.append(f"saved {', '.join(m.refs)}")
    if m.superseded_by:
        parts.append(f"superseded by {', '.join(m.superseded_by)}")
    if m.spans:
        parts.append(f"citation spans {', '.join(m.spans)} stay citable by ID")
    return "; ".join(parts) + "; repeat the call to see it again]"


def arguments_stub(m: Masked) -> dict[str, Any]:
    """The arguments shown for a superseded store call (S2)."""
    saved = ", ".join(m.refs)
    return {
        "masked": f"[arguments hidden: saved {saved}, superseded by {', '.join(m.superseded_by)}]"
    }


def turn_messages(turns: Iterable[Turn], masks: Mapping[str, Masked]) -> list[Message]:
    """Each turn's reply, then each of its tool results, with hidden results stubbed."""
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


def _prefixes(run: str, instance: str, session: Iterable[Segment] = ()) -> tuple[str, ...]:
    own = f"{run}/{instance}/"
    return (own, *dict.fromkeys(f"{s.run}/{s.instance}/" for s in session if s.run != run
                                or s.instance != instance))  # fmt: skip


def _by_me(step: str | None, prefixes: tuple[str, ...]) -> bool:
    return step is not None and step.startswith(prefixes)


def stale_reads(
    view: LogView,
    run: str,
    instance: str,
    pair: ToolPair,
    *,
    blobs: BlobStore | None = None,
    checker: ReadChecker | None = None,
    session: tuple[Segment, ...] = (),
) -> tuple[StaleRead, ...]:
    """The reads of ``pair`` that went out of date since (decision 32).

    Out of date is the invalidation view's answer for the instance's run (ADR-P05 D4,
    S1): ``moved`` (a newer version of the run's line with other content, or a source
    record or query result at another version) or ``gone`` (the version's writer was
    withdrawn, or the record left its source). A newer artifact version this instance,
    or the ``session`` it resumed, saved itself is not stale: it supersedes (rule 3).
    ``checker`` reuses one evaluation of the view across a turn's pairs. A run whose
    agent declared ``off`` or ``edit_guard`` (decision 38) has no stale reads: nothing of
    its context is masked or announced as stale.
    """
    check = checker or ReadChecker(view, blobs=blobs)
    if not masks_stale(check.mode(run)):
        return ()
    mine = _prefixes(run, instance, session)
    out: list[StaleRead] = []
    for read in pair.reads:
        if read.kind in _SOURCE_KINDS:
            status = check.status(run, read)
            if status.state == "gone":
                out.append(StaleRead(read, None))
            elif status.state == "moved" and status.now is not None:
                out.append(StaleRead(read, None, status.now))
            continue
        if read.kind != "artifact":
            continue
        status = check.status(run, read)
        if status.state == "gone":
            out.append(StaleRead(read, None))
            continue
        if status.state != "moved" or status.now is None:
            continue
        newer = check.version(status.now)
        if newer is not None and _by_me(newer.step, mine):
            continue
        out.append(StaleRead(read, ArtifactRef.parse(status.now)))
    return tuple(out)


def _superseded(
    view: LogView, prefixes: tuple[str, ...], pairs: tuple[ToolPair, ...]
) -> dict[str, tuple[tuple[str, ...], bool]]:
    """For each superseded pair: what supersedes it, and whether it was a store call (S2)."""
    out: dict[str, tuple[tuple[str, ...], bool]] = {}
    for i, pair in enumerate(pairs):
        newer: list[str] = []
        stored = False
        for text in pair.saved:
            ref = ArtifactRef.parse(text)
            later = [v for v in versions(view, ref.kind, ref.key) if v.ref.version > ref.version]
            if later:
                newer.append(str(later[-1].ref))
                stored = True
        for later_pair in pairs[i + 1 :]:
            if (later_pair.tool, later_pair.arguments_text) == (pair.tool, pair.arguments_text):
                newer.append(later_pair.step)
                break
        for read in pair.reads:
            if read.kind != "artifact":
                continue
            old = ArtifactRef.from_input(read)
            mine = [
                v
                for v in versions(view, old.kind, old.key)
                if v.ref.version > old.version and _by_me(v.step, prefixes)
            ]
            if mine:
                newer.append(str(mine[-1].ref))
        if newer:
            out[pair.step] = (tuple(dict.fromkeys(newer)), stored)
    return out


def recorded_masks(
    view: LogView, run: str, instance: str, session: tuple[Segment, ...] = ()
) -> Mapping[str, int]:
    """Each tool step an earlier model step of the instance hid, with the turn it began.

    With a ``session`` (decision 36), the masks each segment's own model steps recorded
    count too, so a resumed context hides at least what the session last hid.
    """
    out: dict[str, int] = {}
    for e in view.events:
        if e.type != "step.started" or e.payload.get("kind") != "model" or e.step is None:
            continue
        try:
            sid = StepId.parse(e.step)
        except StepIdInvalid:
            continue
        if sid.turn is None or sid.sub or sid.instance is None:
            continue
        own = sid.run == run and sid.instance == instance
        if not own and not any(s.holds(sid.run, sid.instance, sid.turn) for s in session):
            continue
        for step in e.payload.get("masked", ()):
            out.setdefault(str(step), sid.turn)
    return out


def _why(pair: ToolPair, superseded: Mapping[str, object]) -> MaskReason:
    return "superseded" if pair.step in superseded else "aged"


def _saving(pair: ToolPair, m: Masked) -> int:
    saved = len(canonical_json(pair.text)) - len(canonical_json(render_stub(m)))
    if m.arguments:
        saved += len(pair.arguments_text) - len(canonical_json(arguments_stub(m)))
    return saved


def plan_masking(
    view: LogView,
    run: str,
    instance: str,
    turns: Iterable[Turn],
    policy: ContextPolicy,
    measure: Measure,
    *,
    turn: int,
    recovery: bool = False,
    blobs: BlobStore | None = None,
    session: tuple[Segment, ...] = (),
) -> MaskingPlan:
    """The masking plan of ``instance``'s context for ``turn`` (module doc). Pure.

    ``turns`` are every turn the context shows: a resumed ``session``'s, then the
    instance's own.
    """
    raw = measure(())
    pairs = tuple(p for t in turns for p in t.pairs)
    if not policy.masking or not pairs:
        return MaskingPlan(
            instance, turn, CONTEXT_POLICY_VERSION, raw, raw, (), recovery,
            raw > policy.context_limit_chars,
        )  # fmt: skip
    superseded = _superseded(view, _prefixes(run, instance, session), pairs)
    prior = recorded_masks(view, run, instance, session)
    checker = ReadChecker(view, blobs=blobs)
    stale_of = {
        p.step: stale_reads(view, run, instance, p, checker=checker, session=session) for p in pairs
    }

    def masked(pair: ToolPair, reason: MaskReason, since: int) -> Masked:
        newer, stored = superseded.get(pair.step, ((), False))
        stale = stale_of[pair.step]
        changed = tuple(dict.fromkeys(s.slot_id() for s in stale))
        why: MaskReason = "stale" if stale else reason
        return Masked(
            step=pair.step, tool=pair.tool, chars=len(pair.text), refs=pair.saved,
            spans=pair.spans, reason=why,
            since_turn=since, arguments=stored, superseded_by=newer, changed=changed,
        )  # fmt: skip

    chosen: dict[str, Masked] = {}
    for pair in pairs:
        if stale_of[pair.step]:
            chosen[pair.step] = masked(pair, "stale", prior.get(pair.step, turn))
        elif pair.step in prior:
            chosen[pair.step] = masked(pair, _why(pair, superseded), prior[pair.step])
    estimate = measure(tuple(chosen.values()))
    if estimate > policy.high_water_chars or recovery:
        keep = 1 if recovery else policy.keep_recent_pairs
        target = policy.low_water_chars // 2 if recovery else policy.low_water_chars
        open_pairs = [
            p
            for p in pairs[: max(0, len(pairs) - keep)]
            if p.step not in chosen and p.tool not in policy.exempt_tools
        ]
        ordered = [p for p in open_pairs if p.step in superseded] + [
            p for p in open_pairs if p.step not in superseded
        ]
        for pair in ordered:
            if estimate <= target:
                break
            m = masked(pair, _why(pair, superseded), turn)
            chosen[pair.step] = m
            estimate -= _saving(pair, m)
    plan_masks = tuple(chosen[p.step] for p in pairs if p.step in chosen)
    final = measure(plan_masks) if plan_masks else raw
    return MaskingPlan(
        instance, turn, CONTEXT_POLICY_VERSION, raw, final, plan_masks, recovery,
        final > policy.context_limit_chars,
    )  # fmt: skip
