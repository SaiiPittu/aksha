"""Resume on the existing session or start fresh: one choice per re-entry (decision 36).

When invalidation re-enters a stale writer (ADR-P05 D7, D8), the re-entered instance can
start a **fresh** context (the rebuild summary, decision 34) or **resume** the writer's own
recorded session, with the results that went stale masked in place (decision 32) and the
current versions once at the end. Fresh is the default: a run resumes only when its
``run.started`` sets ``reentry: "resume"`` (a rebuild run inherits the setting of the runs
it rebuilds for, or the opener's explicit one), and even then :func:`choose_reentry` falls
back to fresh on any named trigger. The checks run in this order; the first that fires
decides, and the reason is recorded:

1. ``resume_disabled``: the run's setting is ``fresh``.
2. ``sessions_split``: the invalidated versions were written by more than one instance
   (a rebuild run serving two runs), so a session would merge runs. It never does.
   ``session_missing``: no recorded session, or fewer turns than ``min_prior_turns``.
3. ``definition_changed``: a session run started under another definition hash.
4. ``prior_overflow``, ``prior_looping``, ``prior_failed``: the stop decision after the
   session's last turn judged a context overflow; or the instance ended blocked at the
   turn or stop-veto limit, or repeated one call ``loop_repeats`` times; or it ended
   blocked for another reason.
5. ``tainted_session``: a result in the session carried instruction-like text
   (:func:`instruction_text`) or a recorded ``instruction_text`` flag.
6. ``premise_changed``: a premise of the session moved: a declared read its context
   rendered (unless the kind is ``premise_exempt``), or a person's statement it read or
   was shown that a later answer corrected (a statement span another one ``supersedes``).
7. ``mostly_stale``: the stale marks would hide more than ``max_stale_permille`` of the
   session's characters.
8. ``budget_exceeded``: the session, after its own recorded masks and summaries, plus
   ``headroom_chars`` does not fit under the high watermark.

The session is found from the invalidated version's writer instance, never from a
rebuild run's ``for_runs`` (stress fix F1): its segments are that instance's own turns
since its entry, preceded by the segments it resumed itself. The choice is computed once,
inside the stop decision that re-enters the instance, and recorded on
``task.entered{reentry}`` with ``as_of_seq``, the segments, ``read_runs`` (every run whose
events the context reads, so a replay loads them all: the session's runs, the writer's run,
and every run that saved or reviewed a version of a kind the instance reads), the inputs
new since the session (``new_reads``, F5) and the measured sizes. Assembly reads the
record and never recomputes the choice, so a restart sends the same bytes;
:func:`reentry_divergences` recomputes it to check a replay. Step payloads are read
through the blob store (``blobs``), so an offloaded reply or result is sized, checked and
shown whole.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, get_args

from aksha.runtime.artifacts.blobs import inflate
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.types import ArtifactRef, InputRef, WriterId
from aksha.runtime.artifacts.versions import version
from aksha.runtime.budgets.limits import turns_used
from aksha.runtime.canonical import canonical_json
from aksha.runtime.context.masking import recorded_masks, stale_reads, turn_messages
from aksha.runtime.context.reads import declared_versions
from aksha.runtime.context.summary import latest_compaction, summary_chars
from aksha.runtime.context.turns import instance_turns
from aksha.runtime.context.types import (
    Compaction,
    ContextPolicy,
    Masked,
    Segment,
    ToolPair,
    Turn,
)
from aksha.runtime.events.envelope import Event
from aksha.runtime.execution.errors import StepIdInvalid
from aksha.runtime.execution.types import StepId
from aksha.runtime.grounding.spans import span_index
from aksha.runtime.grounding.types import STATEMENT_SOURCE
from aksha.runtime.invalidation.plan import plan_of
from aksha.runtime.invalidation.types import RebuildBrief, RebuildEntry
from aksha.runtime.invalidation.view import ReadChecker
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.errors import TaskUnknown
from aksha.runtime.tasks.status import run_state
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import FromField, InstanceRef

ReentrySetting = Literal["fresh", "resume"]
"""A run's setting (``run.started{reentry}``): whether its re-entries may resume."""

REENTRY_SETTINGS: frozenset[str] = frozenset(get_args(ReentrySetting))

DEFAULT_SETTING: Final[ReentrySetting] = "fresh"
"""Fresh until the resume-against-fresh evaluation decides otherwise (decision 36)."""

ReentryMode = Literal["fresh", "resume"]

FreshReason = Literal[
    "resume_disabled",
    "session_missing",
    "sessions_split",
    "definition_changed",
    "prior_failed",
    "prior_looping",
    "prior_overflow",
    "tainted_session",
    "premise_changed",
    "mostly_stale",
    "budget_exceeded",
]
FRESH_REASONS: frozenset[str] = frozenset(get_args(FreshReason))

INSTRUCTION_FLAG = "instruction_text"
"""The flag a tool result's recorded value may carry (``value["flags"]``) to mark it."""

_INSTRUCTION_TEXT: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+|your\s+)?"
        r"(previous|prior|above|earlier|preceding)\s+instructions",
        r"\bnote\s+to\s+(ai|llm|language\s+model)s?\b",
        r"\b(ai|llm)\s+(assistants?|agents?)\s+reading\s+this\b",
        r"\bskip\s+(the\s+)?approvals?\b",
        r"\byou\s+are\s+now\s+(a|an|in)\b",
        r"\b(reveal|print|show)\s+(your\s+)?(system\s+prompt|instructions)\b",
    )
)
"""Instruction-like text in a tool result: a fixed, conservative list (decision 36).

Its version is ``CONTEXT_POLICY_VERSION``; it flags, it never filters, and a flagged
session only loses resume. A tool may also flag its own result (``INSTRUCTION_FLAG``).
"""

_LIMIT_CODES = frozenset({"turn_limit", "stop_veto_limit"})


def instruction_text(text: str) -> bool:
    """Whether ``text`` reads like an instruction to the model (module doc, trigger 5)."""
    return any(p.search(text) for p in _INSTRUCTION_TEXT)


@dataclass(frozen=True)
class ReentryChoice:
    """One re-entry's mode and why, as ``task.entered{reentry}`` records it.

    ``from_turn`` is the first turn of the new entry; a fresh context shows only turns
    from it, a resumed one shows ``session`` first. ``as_of_seq`` is the log position the
    choice read. ``session_turns``, ``session_chars`` and ``stale_chars`` measure the
    session whether or not it was resumed, so an evaluation can compare the modes.
    """

    mode: ReentryMode
    reason: FreshReason | None
    setting: ReentrySetting
    from_turn: int
    as_of_seq: int
    session: tuple[Segment, ...] = ()
    read_runs: tuple[str, ...] = ()
    new_reads: tuple[str, ...] = ()
    session_turns: int = 0
    session_chars: int = 0
    stale_chars: int = 0

    def __post_init__(self) -> None:
        """Refuse a resume with a reason, a fresh without one, or an unknown reason."""
        if (self.mode == "resume") != (self.reason is None):
            raise ValueError("a resume has no fresh reason, and a fresh re-entry has one")
        if self.reason is not None and self.reason not in FRESH_REASONS:
            raise ValueError(f"unknown fresh reason {self.reason!r}")

    def as_json(self) -> dict[str, Any]:
        """The choice as JSON data."""
        out: dict[str, Any] = {
            "mode": self.mode,
            "setting": self.setting,
            "from_turn": self.from_turn,
            "as_of_seq": self.as_of_seq,
            "session": [s.as_json() for s in self.session],
            "read_runs": list(self.read_runs),
            "new_reads": list(self.new_reads),
            "session_turns": self.session_turns,
            "session_chars": self.session_chars,
            "stale_chars": self.stale_chars,
        }
        if self.reason is not None:
            out["reason"] = self.reason
        return out

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ReentryChoice:
        """Read :meth:`as_json`'s form back."""
        return cls(
            mode=value["mode"],
            reason=value.get("reason"),
            setting=value["setting"],
            from_turn=int(value["from_turn"]),
            as_of_seq=int(value["as_of_seq"]),
            session=tuple(Segment.from_json(s) for s in value.get("session", ())),
            read_runs=tuple(str(r) for r in value.get("read_runs", ())),
            new_reads=tuple(str(r) for r in value.get("new_reads", ())),
            session_turns=int(value.get("session_turns", 0)),
            session_chars=int(value.get("session_chars", 0)),
            stale_chars=int(value.get("stale_chars", 0)),
        )


# --- the setting ---------------------------------------------------------------------


def reentry_setting(view: LogView, run: str) -> ReentrySetting:
    """``run``'s setting from its ``run.started``; ``fresh`` when it names none."""
    for e in view.events:
        if e.type == "run.started" and e.run == run:
            value = e.payload.get("reentry", DEFAULT_SETTING)
            return "resume" if value == "resume" else "fresh"
    return DEFAULT_SETTING


def inherited_setting(view: LogView, for_runs: Iterable[str]) -> ReentrySetting:
    """A rebuild run's setting when its opener names none.

    ``resume`` only if every run it rebuilds for chose ``resume``; else ``fresh``.
    """
    runs = list(for_runs)
    if runs and all(reentry_setting(view, r) == "resume" for r in runs):
        return "resume"
    return DEFAULT_SETTING


# --- the recorded entry --------------------------------------------------------------


def entry_event(view: LogView, ref: InstanceRef) -> Event | None:
    """The instance's latest ``task.entered`` in its run, or None."""
    found: Event | None = None
    for e in view.events:
        if (
            e.type == "task.entered"
            and e.run == ref.run
            and e.payload.get("task") == ref.task_path
            and e.payload.get("key") == ref.key
        ):
            found = e
    return found


def recorded_choice(view: LogView, ref: InstanceRef) -> ReentryChoice | None:
    """The choice recorded on the instance's latest entry, or None (a first entry)."""
    e = entry_event(view, ref)
    if e is None or "reentry" not in e.payload:
        return None
    return ReentryChoice.from_json(e.payload["reentry"])


# --- the session ---------------------------------------------------------------------


@dataclass(frozen=True)
class SessionBlock:
    """One segment's turns as a context shows them: its recorded summary, then the rest."""

    segment: Segment
    compaction: Compaction | None
    turns: tuple[Turn, ...]


def session_blocks(
    view: LogView, session: Iterable[Segment], *, blobs: BlobStore | None = None
) -> tuple[SessionBlock, ...]:
    """Each segment's turns and the newest summary made inside it, oldest segment first."""
    out: list[SessionBlock] = []
    for s in session:
        c = latest_compaction(
            view, s.run, s.instance, since_turn=s.from_turn, through_turn=s.through_turn
        )
        covered = c.through_turn if c is not None else s.from_turn - 1
        turns = tuple(
            t
            for t in instance_turns(view, s.run, s.instance, blobs=blobs)
            if max(covered, s.from_turn - 1) < t.number <= s.through_turn
        )
        out.append(SessionBlock(s, c, turns))
    return tuple(out)


def _entries(view: LogView, tree: CompiledTree, ref: InstanceRef) -> tuple[RebuildEntry, ...]:
    """The plan's entries that re-enter ``ref``'s writer, from one plan computation."""
    writer = WriterId(tree.agent, ref.task_path, ref.key)
    return tuple(e for e in plan_of(view) if e.writer == writer)


def _origins(view: LogView, entries: Iterable[RebuildEntry]) -> tuple[tuple[str, str], ...]:
    """The instances that wrote the versions the plan re-enters an instance for (F1)."""
    out: list[tuple[str, str]] = []
    for entry in entries:
        found = version(view, entry.invalidated)
        if found is None or found.step is None:
            continue
        try:
            sid = StepId.parse(found.step)
        except StepIdInvalid:
            continue
        if sid.instance is not None and (sid.run, sid.instance) not in out:
            out.append((sid.run, sid.instance))
    return tuple(out)


def _session_of(
    view: LogView, run: str, instance: str, *, blobs: BlobStore | None = None
) -> tuple[Segment, ...]:
    """The session an instance's context showed through its last turn.

    What it resumed itself (if it did), then its own turns since its entry.
    """
    ref = InstanceRef.parse(run, instance)
    prior = recorded_choice(view, ref)
    turns = instance_turns(view, run, instance, blobs=blobs)
    start = 1 if prior is None else prior.from_turn
    own = [t.number for t in turns if t.number >= start]
    segments: list[Segment] = []
    if prior is not None and prior.mode == "resume":
        segments.extend(prior.session)
    if own:
        segments.append(Segment(run, instance, start, max(own)))
    return tuple(segments)


def _definition(view: LogView, run: str) -> str | None:
    for e in view.events:
        if e.type == "run.started" and e.run == run:
            value = e.payload.get("definition_hash")
            return None if value is None else str(value)
    return None


def _prior_end(
    view: LogView, tree: CompiledTree, run: str, instance: str, pairs: Iterable[ToolPair],
    loop_repeats: int,
) -> FreshReason | None:  # fmt: skip
    """``prior_overflow``, ``prior_looping`` or ``prior_failed`` for the session's instance.

    An overflow is read from the stop decision's candidate, not its ``reason``: the leaf's
    FAIL pops it and the walk goes on, so the record's reason is the final verdict's.
    """
    if _last_turn_overflowed(view, run, instance):
        return "prior_overflow"
    ref = InstanceRef.parse(run, instance)
    last_fail: Event | None = None
    for e in view.events:
        if e.type != "stop.decided" or e.run != run:
            continue
        pops = e.payload.get("pops") or ()
        if any(list(p) == [instance, "fail"] for p in pops):
            last_fail = e
    try:
        blocked = run_state(view, tree, run).status(ref) == "blocked"
    except TaskUnknown:  # a Task the tree no longer has: definition_changed judges it
        blocked = False
    if blocked and last_fail is not None:
        if last_fail.payload.get("limit") in _LIMIT_CODES:
            return "prior_looping"
        return "prior_failed"
    if blocked:
        return "prior_failed"
    repeats = Counter((p.tool, p.arguments_text) for p in pairs)
    if repeats and max(repeats.values()) >= loop_repeats:
        return "prior_looping"
    return None


def _tainted(view: LogView, pairs: Iterable[ToolPair], blobs: BlobStore | None) -> bool:
    steps = {p.step: p for p in pairs}
    for e in view.events:
        if e.type == "step.completed" and e.step in steps:
            value = inflate(e.payload, blobs).get("value")
            if isinstance(value, Mapping) and INSTRUCTION_FLAG in (value.get("flags") or ()):
                return True
    return any(instruction_text(p.text) for p in steps.values())


def _session_reads(
    view: LogView, session: tuple[Segment, ...], via: str | None = None
) -> tuple[InputRef, ...]:
    out: list[InputRef] = []
    for e in view.events:
        if e.type != "read.recorded" or e.step is None:
            continue
        if via is not None and e.payload.get("via") != via:
            continue
        try:
            sid = StepId.parse(e.step)
        except StepIdInvalid:
            continue
        if sid.instance is None or sid.turn is None:
            continue
        if any(s.holds(sid.run, sid.instance, sid.turn) for s in session):
            out.append(InputRef.from_json(e.payload["input"]))
    return tuple(dict.fromkeys(out))


def _premise_moved(
    view: LogView,
    run: str,
    session: tuple[Segment, ...],
    exempt: frozenset[str],
    check: ReadChecker,
) -> bool:
    """Whether a declared read the session's context rendered, or a statement, moved."""
    rendered = [r for r in _session_reads(view, session, via="context") if r.kind == "artifact"]
    for read in rendered:
        if ArtifactRef.from_input(read).kind in exempt:
            continue
        if check.status(run, read).state in ("moved", "gone"):
            return True
    return bool(corrected_statements(view, session))


def corrected_statements(view: LogView, session: Iterable[Segment]) -> tuple[str, ...]:
    """The person's statements the session read or was shown that a later answer corrected.

    A statement is a span of source ``statement`` (an interrupt answer, ADR-P13). The
    session read it when a ``read.recorded`` of kind ``statement`` inside one of its
    turns names the span (its ID, or its interrupt and answer as ``id`` and ``version``),
    or was shown it when the span's ``shown_to`` holds a segment's instance. It moved
    when a later statement span ``supersedes`` it. Returns the moved span IDs, sorted.
    """
    segments = tuple(session)
    if not segments:
        return ()
    index = span_index(view)
    replaced = index.superseded()
    if not replaced:
        return ()
    owners = {(s.run, s.instance) for s in segments}
    statements = [sp for sp in index.spans.values() if sp.source == STATEMENT_SOURCE]
    by_answer = {(sp.record_id, sp.version): sp.span_id for sp in statements}
    seen = {sp.span_id for sp in statements if sp.shown_to & owners}
    for read in _session_reads(view, segments):
        if read.kind == "statement":
            seen.add(by_answer.get((read.id, read.version), read.id))
    return tuple(sorted(seen & replaced))


def _sizes(
    view: LogView, run: str, instance: str, blocks: tuple[SessionBlock, ...],
    session: tuple[Segment, ...], checker: ReadChecker,
) -> tuple[int, int]:  # fmt: skip
    """The session's characters, and the part a resumed context would hide as stale.

    The session is measured with its own recorded masks and summaries; each stale
    result's body is counted whole.
    """
    prior = recorded_masks(view, run, instance, session)
    total = 0
    stale = 0
    for block in blocks:
        if block.compaction is not None:
            total += summary_chars(block.compaction)
        masks: dict[str, Masked] = {}
        for t in block.turns:
            for p in t.pairs:
                if p.step in prior:
                    masks[p.step] = _aged(p)
                elif stale_reads(view, run, instance, p, checker=checker, session=session):
                    stale += len(canonical_json(p.text))
        messages = turn_messages(block.turns, masks)
        total += sum(len(canonical_json(m.as_json())) for m in messages)
    return total, stale


def _aged(pair: ToolPair) -> Masked:
    return Masked(
        step=pair.step, tool=pair.tool, chars=len(pair.text), refs=pair.saved, spans=pair.spans,
        reason="aged", since_turn=pair.turn,
    )  # fmt: skip


def new_reads(
    view: LogView, tree: CompiledTree, ref: InstanceRef, seen: Iterable[InputRef]
) -> tuple[str, ...]:
    """Slots a read of every key (or of a listed field) names now that ``seen`` never read.

    Stress fix F5: a new key that entered a declared read since the session (or the old
    version) read it, such as a new record in a collection, is named in the notice.
    """
    reads = tree.reads(ref.task_path)
    if not any(r.keys != "own" for r in reads):
        return ()
    known = {ArtifactRef.from_input(r).slot_id() for r in seen if r.kind == "artifact"}
    out: list[str] = []
    for v in declared_versions(view, tree, ref):
        own = any(r.keys == "own" and r.kind == v.ref.kind for r in reads)
        if not own and v.ref.slot_id() not in known:
            out.append(str(v.ref))
    return tuple(out)


def choose_reentry(
    view: LogView,
    tree: CompiledTree,
    ref: InstanceRef,
    policy: ContextPolicy,
    *,
    blobs: BlobStore | None = None,
) -> ReentryChoice:
    """The re-entry mode of ``ref`` now (module doc). Pure: it reads only ``view``.

    ``blobs`` inflates offloaded step payloads, so the session is read as it was sent.
    """
    setting = reentry_setting(view, ref.run)
    from_turn = turns_used(view, ref.run, ref.instance) + 1
    entries = _entries(view, tree, ref)
    origins = _origins(view, entries)
    session: tuple[Segment, ...] = ()
    if len(origins) == 1:
        session = _session_of(view, *origins[0], blobs=blobs)
    blocks = session_blocks(view, session, blobs=blobs)
    pairs = tuple(p for b in blocks for t in b.turns for p in t.pairs)
    count = sum(len(b.turns) for b in blocks)
    seen = (*_session_reads(view, session), *_old_reads(view, entries))
    suppliers = supplier_runs(view, _read_kinds(tree, ref, seen))
    runs = tuple(sorted({ref.run, *(r for r, _ in origins), *(s.run for s in session), *suppliers}))
    fresh_runs = tuple(sorted({ref.run, *(r for r, _ in origins), *suppliers}))
    checker = ReadChecker(view, blobs=blobs)
    chars, stale = _sizes(view, ref.run, ref.instance, blocks, session, checker)
    rp = policy.resume

    def made(reason: FreshReason | None) -> ReentryChoice:
        return ReentryChoice(
            mode="resume" if reason is None else "fresh",
            reason=reason,
            setting=setting,
            from_turn=from_turn,
            as_of_seq=view.seq_read,
            session=session if reason is None else (),
            read_runs=runs if reason is None else fresh_runs,
            new_reads=new_reads(view, tree, ref, seen),
            session_turns=count,
            session_chars=chars,
            stale_chars=stale,
        )

    if setting != "resume":
        return made("resume_disabled")
    if len(origins) > 1:
        return made("sessions_split")
    if not origins or count < max(1, rp.min_prior_turns):
        return made("session_missing")
    here = _definition(view, ref.run)
    if any(_definition(view, s.run) != here for s in session):
        return made("definition_changed")
    last = session[-1]
    ended = _prior_end(view, tree, last.run, last.instance, pairs, rp.loop_repeats)
    if ended is not None:
        return made(ended)
    if _tainted(view, pairs, blobs):
        return made("tainted_session")
    if _premise_moved(view, ref.run, session, rp.premise_exempt, checker):
        return made("premise_changed")
    if chars and stale * 1000 > rp.max_stale_permille * chars:
        return made("mostly_stale")
    if chars - stale + rp.headroom_chars > policy.high_water_chars:
        return made("budget_exceeded")
    return made(None)


def _read_kinds(tree: CompiledTree, ref: InstanceRef, seen: Iterable[InputRef]) -> frozenset[str]:
    """The artifact kinds the choice and the context read.

    The declared reads, the kinds their keys are listed in, and the kind of every artifact
    version the session or the old version read.
    """
    kinds: set[str] = set()
    for read in tree.reads(ref.task_path):
        kinds.add(read.kind)
        if isinstance(read.keys, FromField):
            kinds.add(read.keys.kind)
    kinds.update(ArtifactRef.from_input(r).kind for r in seen if r.kind == "artifact")
    return frozenset(kinds)


def supplier_runs(view: LogView, kinds: Iterable[str]) -> frozenset[str]:
    """The runs whose events decide which version of ``kinds`` is current (F10).

    A version is current by its ``artifact.saved`` and by the diff reviews that propose or
    accept a revision of it (ADR-P05 D1), so the runs of all three are listed. Without them
    a replay scoped to :func:`runs_to_load` reads an older or no version of a declared read
    whose writer ran in a third run, and the recomputed choice diverges.
    """
    wanted = frozenset(kinds)
    out: set[str] = set()
    for e in view.events:
        if e.run is None:
            continue
        if e.type == "artifact.saved":
            if e.payload.get("kind") in wanted:
                out.add(e.run)
        elif (e.type == "interrupt.raised" and e.payload.get("kind") == "review") or (
            e.type == "review.accepted"
        ):
            revision = str(e.payload.get("revision", ""))
            try:
                kind = ArtifactRef.parse(revision).kind
            except ValueError:
                continue
            if kind in wanted:
                out.add(e.run)
    return frozenset(out)


def _old_reads(view: LogView, entries: Iterable[RebuildEntry]) -> tuple[InputRef, ...]:
    """The reads of the versions the plan re-enters an instance for: what fresh knew."""
    out: list[InputRef] = []
    for entry in entries:
        found = version(view, entry.invalidated)
        if found is not None:
            out.extend(found.reads)
    return tuple(out)


def _last_turn_overflowed(view: LogView, run: str, instance: str) -> bool:
    """Whether the stop decision after the instance's last turn judged an overflow."""
    for e in reversed(view.events):
        p = e.payload
        if (
            e.type == "stop.decided"
            and e.run == run
            and p.get("candidate_instance") == instance
            and p.get("after_turn") is not None
        ):
            return bool(p.get("candidate") == "overflow")
    return False


# --- the notice ----------------------------------------------------------------------


def reentry_notice(state: RebuildBrief | None, choice: ReentryChoice, ref: InstanceRef) -> str:
    """What a resumed instance reads between its session and its new turns (decision 36).

    Written by the runtime from the rebuild summary at entry (``state``), never by a model:
    which earlier session this is, the output that went out of date, the reads that
    changed, the inputs that are new, and the rule that stale results are hidden.
    """
    spans = ", ".join(
        f"{s.run} {s.instance} turns {s.from_turn} to {s.through_turn}" for s in choice.session
    )
    lines = [
        f"[runtime: re-entry of {ref.instance}] You are continuing your earlier session ({spans})."
    ]
    if state is not None:
        lines.append(f"Your output {state.old} is out of date. Produce a new version of it.")
        if state.changed:
            lines.append("Inputs that changed since you read them:")
            for r in state.changed:
                now = r.now if r.now is not None else "gone"
                lines.append(
                    f"- {r.input.kind} {r.input.id}@{r.input.version} ({r.state}) is now {now}"
                )
        if state.statements:
            lines += [
                "The person already answered these; do not ask again:",
                *(f"- {r}" for r in state.statements),
            ]
    if choice.new_reads:
        lines += ["New inputs since your last turn:", *(f"- {r}" for r in choice.new_reads)]
    lines.append(
        "Earlier results that read a changed input are hidden as [superseded] placeholders; "
        "do not quote them. Their current versions are in the change notice at the end."
    )
    return "\n".join(lines)


# --- replay --------------------------------------------------------------------------


def runs_to_load(view: LogView, run: str) -> frozenset[str]:
    """``run`` and every run its re-entries read, transitively (stress fix F10).

    A replay or an evaluation's scratch store that loads only ``run``'s own events cannot
    rebuild a resumed context: the session lives in the runs ``read_runs`` names.
    """
    out = {run}
    queue = [run]
    while queue:
        r = queue.pop()
        for e in view.events:
            if e.type == "task.entered" and e.run == r and "reentry" in e.payload:
                for other in e.payload["reentry"].get("read_runs", ()):
                    if other not in out:
                        out.add(str(other))
                        queue.append(str(other))
    return frozenset(out)


def scoped_view(view: LogView, runs: Iterable[str]) -> LogView:
    """``view`` with only the events of ``runs`` and the owner-level events (no run).

    A filtered view is never ``complete``, so no projection cache keeps its state.
    """
    keep = frozenset(runs)
    events = tuple(e for e in view.events if e.run is None or e.run in keep)
    return LogView(view.owner, view.seq_read, events, view.artifacts, complete=False)


@dataclass(frozen=True)
class Divergence:
    """A recorded re-entry choice that a recomputation at its ``as_of_seq`` does not match."""

    run: str
    instance: str
    recorded: ReentryChoice
    recomputed: ReentryChoice


def reentry_divergences(
    view: LogView,
    tree: CompiledTree,
    policy: ContextPolicy,
    *,
    blobs: BlobStore | None = None,
) -> tuple[Divergence, ...]:
    """Every recorded choice of ``tree``'s agent that differs when recomputed (F10).

    Each ``task.entered{reentry}`` is recomputed over ``view`` as it stood at the
    recorded ``as_of_seq``. Any field that changes what is sent or what a replay must
    load is compared: the mode, reason, setting, first turn, session, read runs and new
    reads (the measured sizes are not, they only describe the session). Run it over a
    view that holds :func:`runs_to_load` of the run being replayed.
    """
    agents = {
        e.run: e.payload.get("agent") for e in view.events if e.type == "run.started" and e.run
    }
    out: list[Divergence] = []
    for e in view.events:
        if e.type != "task.entered" or "reentry" not in e.payload or e.run is None:
            continue
        if agents.get(e.run) != tree.agent or e.instance is None:
            continue
        recorded = ReentryChoice.from_json(e.payload["reentry"])
        ref = InstanceRef.parse(e.run, e.instance)
        again = choose_reentry(view.upto(recorded.as_of_seq), tree, ref, policy, blobs=blobs)
        if _compared(again) != _compared(recorded):
            out.append(Divergence(e.run, e.instance, recorded, again))
    return tuple(out)


def _compared(c: ReentryChoice) -> tuple[object, ...]:
    return (c.mode, c.reason, c.setting, c.from_turn, c.session, c.read_runs, c.new_reads)
