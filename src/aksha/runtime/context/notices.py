"""The runtime's own messages in a context, each rendered from the event store (ADR-P08).

1. :func:`recite`: the recitation block, shown only once a result is masked (D5): the
   Task's goal, its completion check results, what it saved, the inputs that went out of
   date, and how to see a hidden result again.
2. :func:`change_notice`: the current versions of the artifacts a stale result read,
   once, at the end of the context (decision 32).
3. :func:`rebuild_notice`: a fresh re-entry's context starts from the rebuild summary
   (``RebuildBrief``; ADR-P05 D10, decisions 34 and 36): the old version, what changed,
   what to read instead, what the person already answered and the inputs that are new
   since. :func:`rebuild_brief_for` reads it from the log as it stood when the instance
   was re-entered, so it never changes mid-entry. A resumed re-entry gets none; its
   notice is ``reentry.reentry_notice``.
4. :func:`answers` (interrupt answers for this instance, never reduced) and
   :func:`continue_reason` (why the runtime refused the last finish).
"""

from __future__ import annotations

from collections.abc import Iterable

from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.types import ArtifactVersion, WriterId
from aksha.runtime.artifacts.versions import version
from aksha.runtime.canonical import canonical_json
from aksha.runtime.context.types import ContextPolicy, Masked, StaleRead
from aksha.runtime.events.envelope import Event
from aksha.runtime.grounding.types import STATEMENT_SOURCE
from aksha.runtime.invalidation.plan import rebuild_plan
from aksha.runtime.invalidation.rebuild import rebuild_brief
from aksha.runtime.invalidation.types import RebuildBrief
from aksha.runtime.invalidation.view import invalidation_view
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.types import InstanceRef
from aksha.runtime.tools.results import cap_text


def _checks(view: LogView, instance: InstanceRef) -> list[str]:
    latest: dict[str, str] = {}
    for e in view.events:
        p = e.payload
        if (
            e.type == "completion_check.evaluated"
            and e.run == instance.run
            and p.get("task") == instance.task_path
            and p.get("key") == instance.key
        ):
            name = str(p.get("check") or p.get("on"))
            state = "passed" if p.get("ok") else f"failed: {p.get('message', '')}"
            latest[name] = state
    return [f"- {name}: {state}" for name, state in latest.items()]


def _outputs(view: LogView, instance: InstanceRef) -> list[str]:
    prefix = f"{instance.run}/{instance.instance}/"
    slots: dict[str, str] = {}
    for e in view.events:
        if e.type == "artifact.saved" and e.step is not None and e.step.startswith(prefix):
            ref = str(e.payload["ref"])
            slots[ref.rsplit("@", 1)[0]] = ref
    return [f"- {ref}" for ref in slots.values()]


def recite(
    view: LogView,
    instance: InstanceRef,
    masked: Iterable[Masked],
    stale: Iterable[StaleRead],
    *,
    brief: str = "",
) -> str:
    """The recitation block (module doc, point 1)."""
    hidden = list(masked)
    lines = [
        f"Recitation for Task {instance.task_path}" + (f" [{instance.key}]" if instance.key else "")
    ]
    if brief:
        lines += ["Goal:", brief]
    checks = _checks(view, instance)
    lines += ["Completion checks:", *(checks or ["- none evaluated yet"])]
    outputs = _outputs(view, instance)
    lines += ["Saved so far:", *(outputs or ["- nothing yet"])]
    stale_list = list(stale)
    if stale_list:
        lines += ["Out of date since you read them:"]
        lines += [f"- {s.read.id}@{s.read.version} is now {s.now_text()}" for s in stale_list]
    lines.append(
        f"{len(hidden)} earlier tool results are hidden above to save space; "
        "repeat a call to see its result again."
    )
    return "\n".join(lines)


def change_notice(
    view: LogView,
    stale: Iterable[StaleRead],
    policy: ContextPolicy,
    *,
    blobs: BlobStore | None = None,
) -> tuple[str, tuple[ArtifactVersion, ...]]:
    """The change notice and the versions it shows (module doc, point 2).

    An artifact read shows its current version's content; a source record or query
    result (decision 36) is named with the version it moved to, since the runtime holds
    no copy of it: the instance reads it again through its tools.
    """
    lines = ["Change notice: these inputs changed after you read them. Their current versions:"]
    shown: list[ArtifactVersion] = []
    seen: set[str] = set()
    for s in stale:
        label = s.now_text() if not s.gone else f"gone:{s.read.kind}:{s.read.id}"
        if label in seen:
            continue
        seen.add(label)
        if s.gone:
            lines.append(f"- {s.read.id}@{s.read.version} is gone: its writer was withdrawn")
            continue
        if s.now is None:
            lines.append(
                f"- {s.read.kind} {s.read.id}@{s.read.version} is now version {s.moved_to}; "
                "read it again with your tools"
            )
            continue
        now = version(view, s.now, blobs=blobs)
        if now is None:
            continue
        body, _ = cap_text(canonical_json(now.content), policy.read_cap_chars)
        lines += [f"- {s.read.id}@{s.read.version} is now {s.now}:", body]
        shown.append(now)
    return "\n".join(lines), tuple(shown)


def rebuild_entry(view: LogView, instance: InstanceRef) -> Event | None:
    """The instance's current entry when it is a ``task.entered{cause: rebuild}``, or None.

    Only the latest ``task.entered`` of the instance in its run counts: a rebuild summary
    belongs to its own entry, and a later entry of any cause ends it.
    """
    found: Event | None = None
    for e in view.events:
        if (
            e.type == "task.entered"
            and e.run == instance.run
            and e.payload.get("task") == instance.task_path
            and e.payload.get("key") == instance.key
        ):
            found = e
    if found is None or found.payload.get("cause") != "rebuild":
        return None
    return found


def rebuild_at_entry(
    view: LogView, entry: Event, instance: InstanceRef, *, blobs: BlobStore | None = None
) -> RebuildBrief | None:
    """The rebuild summary of ``instance`` as the log stood at ``entry`` (its re-entry)."""
    agent: str | None = None
    for e in view.events:
        if e.type == "run.started" and e.run == instance.run:
            agent = str(e.payload["agent"])
            break
    if agent is None:
        return None
    at_entry = view.upto(entry.seq)
    iv = invalidation_view(at_entry, blobs=blobs)
    writer = WriterId(agent, instance.task_path, instance.key)
    for plan_entry in rebuild_plan(iv):
        if plan_entry.writer == writer:
            return rebuild_brief(at_entry, iv, plan_entry)
    return None


def rebuild_brief_for(
    view: LogView, instance: InstanceRef, *, blobs: BlobStore | None = None
) -> RebuildBrief | None:
    """The rebuild summary of a fresh re-entry of ``instance``, as of its entry; else None.

    A re-entry recorded with a mode (decision 36) gets it when the mode is
    ``fresh``, in a rebuild run or in place; one recorded before modes existed gets it
    only in a rebuild run, as it always did.
    """
    entry = rebuild_entry(view, instance)
    if entry is None:
        return None
    recorded = entry.payload.get("reentry")
    if recorded is not None:
        if recorded.get("mode") != "fresh":
            return None
    elif not _rebuild_run(view, instance.run):
        return None
    return rebuild_at_entry(view, entry, instance, blobs=blobs)


def _rebuild_run(view: LogView, run: str) -> bool:
    for e in view.events:
        if e.type == "run.started" and e.run == run:
            return e.payload.get("cause") == "rebuild"
    return False


def rebuild_notice(
    summary: RebuildBrief, policy: ContextPolicy, new_reads: Iterable[str] = ()
) -> str:
    """The rebuild summary as the first message of a fresh context (module doc, point 3).

    ``new_reads`` (decision 36) are the inputs that entered a read of every key since the
    old version was written; they are listed so the instance knows they are new.
    """
    body, _ = cap_text(canonical_json(summary.content), policy.read_cap_chars)
    lines = [
        f"Rebuild: your earlier version {summary.old} is out of date. It was:",
        body,
        "What changed:",
    ]
    for r in summary.changed:
        now = r.now if r.now is not None else "gone"
        lines.append(f"- {r.input} ({r.state}) is now {now}")
    if summary.current_reads:
        lines += ["Read these instead:", *(f"- {r}" for r in summary.current_reads)]
    if summary.statements:
        lines += [
            "The person already answered these; do not ask again:",
            *(f"- {r}" for r in summary.statements),
        ]
    added = list(new_reads)
    if added:
        lines += ["New inputs since then:", *(f"- {r}" for r in added)]
    return "\n".join(lines)


def _statement_spans(view: LogView) -> dict[tuple[str, str], str]:
    """The statement span minted for each answered question, by (interrupt, question ID)."""
    out: dict[tuple[str, str], str] = {}
    for e in view.events:
        if e.type != "excerpts.minted":
            continue
        for s in e.payload.get("spans", ()):
            if s.get("source") == STATEMENT_SOURCE:
                qid = str(s["version"]).rsplit("/", 1)[-1]
                out[(str(s["record_id"]), qid)] = str(s["span_id"])
    return out


def answers(view: LogView, instance: InstanceRef) -> str:
    """The person's answers to this instance's interrupts, in order, never reduced.

    Each answer minted as a statement span (ADR-P13 D4) names that span, so the model can
    cite the person's words with the statement form.
    """
    questions: dict[str, dict[str, str]] = {}
    lines: list[str] = []
    statements = _statement_spans(view)
    for e in view.events:
        if e.run != instance.run:
            continue
        if e.type == "interrupt.raised" and e.instance == instance.instance:
            qs = {str(q["id"]): str(q["text"]) for q in e.payload.get("questions", ())}
            questions[str(e.payload["interrupt"])] = qs
        elif e.type == "interrupt.resolved" and str(e.payload["interrupt"]) in questions:
            ident = str(e.payload["interrupt"])
            qs = questions[ident]
            for qid, answer in sorted(dict(e.payload.get("answers", {})).items()):
                line = f"Q: {qs.get(qid, qid)}\nA: {answer}"
                span = statements.get((ident, qid))
                lines.append(f"{line}\n(cite this answer as statement {span})" if span else line)
            if "decision" in e.payload:
                lines.append(f"Decision on {e.payload['interrupt']}: {e.payload['decision']}")
    if not lines:
        return ""
    return "Answers from the person:\n" + "\n".join(lines)


def continue_reason(view: LogView, instance: InstanceRef, turn: int) -> str:
    """Why the runtime refused the instance's finish after the previous turn, if it did."""
    reason = ""
    for e in view.events:
        p = e.payload
        if (
            e.type == "stop.decided"
            and e.run == instance.run
            and p.get("candidate_instance") == instance.instance
            and p.get("after_turn") == turn - 1
        ):
            reason = str(p.get("reason", "")) if p.get("verdict") == "continue" else ""
    return f"The runtime did not accept your finish: {reason}" if reason else ""
