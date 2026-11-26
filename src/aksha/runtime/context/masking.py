"""Observation masking (ADR-P08 D5, S2, decision 32): which tool results a context hides.

A masked result's body is replaced by a stub naming the step, its size, what it saved
and the spans it minted; the call and its arguments stay, so a tool pair is never split.
The plan for a turn is a pure function of the log and the policy:

1. **Stale** results are hidden first and always (decision 32): a result that read an
   artifact version the invalidation view now reads as ``moved`` or ``gone`` for the
   instance's run (ADR-P05 D4: the sibling rule and early cutoff apply, so another run's
   version and an equal rebuild hide nothing), unless this instance saved the newer
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

With nothing hidden the context is byte for byte the one built with masking off.
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
    StaleRead,
    ToolPair,
    Turn,
)
from aksha.runtime.execution.errors import StepIdInvalid
from aksha.runtime.execution.types import StepId
from aksha.runtime.invalidation.view import ReadChecker
from aksha.runtime.projections.view import LogView

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


def _prefix(run: str, instance: str) -> str:
    return f"{run}/{instance}/"


def _by_me(step: str | None, run: str, instance: str) -> bool:
    return step is not None and step.startswith(_prefix(run, instance))


def stale_reads(
    view: LogView,
    run: str,
    instance: str,
    pair: ToolPair,
    *,
    blobs: BlobStore | None = None,
    checker: ReadChecker | None = None,
) -> tuple[StaleRead, ...]:
    """The artifact reads of ``pair`` that went out of date since (decision 32).

    Out of date is the invalidation view's answer for the instance's run (ADR-P05 D4,
    S1): ``moved`` (a newer version of the run's line with other content) or ``gone``
    (the version's writer was withdrawn). A newer version this instance saved itself is
    not stale: it supersedes (rule 3). ``checker`` reuses one evaluation of the view
    across a turn's pairs.
    """
    check = checker or ReadChecker(view, blobs=blobs)
    out: list[StaleRead] = []
    for read in pair.reads:
        if read.kind != "artifact":
            continue
        status = check.status(run, read)
        if status.state == "gone":
            out.append(StaleRead(read, None))
            continue
        if status.state != "moved" or status.now is None:
            continue
        newer = check.version(status.now)
        if newer is not None and _by_me(newer.step, run, instance):
            continue
        out.append(StaleRead(read, ArtifactRef.parse(status.now)))
    return tuple(out)


def _superseded(
    view: LogView, run: str, instance: str, pairs: tuple[ToolPair, ...]
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
                if v.ref.version > old.version and _by_me(v.step, run, instance)
            ]
            if mine:
                newer.append(str(mine[-1].ref))
        if newer:
            out[pair.step] = (tuple(dict.fromkeys(newer)), stored)
    return out


def recorded_masks(view: LogView, run: str, instance: str) -> Mapping[str, int]:
    """Each tool step an earlier model step of the instance hid, with the turn it began."""
    out: dict[str, int] = {}
    for e in view.events:
        if e.type != "step.started" or e.payload.get("kind") != "model" or e.step is None:
            continue
        try:
            sid = StepId.parse(e.step)
        except StepIdInvalid:
            continue
        if sid.run != run or sid.instance != instance or sid.turn is None or sid.sub:
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
) -> MaskingPlan:
    """The masking plan of ``instance``'s context for ``turn`` (module doc). Pure."""
    raw = measure(())
    pairs = tuple(p for t in turns for p in t.pairs)
    if not policy.masking or not pairs:
        return MaskingPlan(
            instance, turn, CONTEXT_POLICY_VERSION, raw, raw, (), recovery,
            raw > policy.context_limit_chars,
        )  # fmt: skip
    superseded = _superseded(view, run, instance, pairs)
    prior = recorded_masks(view, run, instance)
    checker = ReadChecker(view, blobs=blobs)
    stale_of = {p.step: stale_reads(view, run, instance, p, checker=checker) for p in pairs}

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
