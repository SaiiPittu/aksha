"""A person's choice as an artifact (ADR-P04 choice binding, ADR-P17 D2): the plan and the check.

A Task that declares ``DeclaredInterrupt("choice", ..., options=..., saves=AnswerSave(...))``
does not let the model decide. The options are built by code from the Task's view; the
model may only ask, with a one-line summary; the person answers; and the runtime, not the
model, saves the kind from the answer (``loop/answers.py``). This module
holds the two pure halves:

1. :func:`answer_save_plan`: what the runtime saves at the start of the instance's next
   turn, if anything. In order:

   - the newest resolved choice of this instance (or one raised for its writer key, such
     as the fresh choice after a rejected review) that no version names yet: the codes
     the person chose, origin ``choice``. Not when it is stale (:func:`is_stale`: a read
     its options were drawn from has a newer version, or the options now differ), and not
     when a runtime save of it was already refused: then nothing, and the person is asked
     again (the stop decision re-enters the instance once for a stale answer);
   - nothing more once the instance saved since it was last entered;
   - in a rebuild, when the slot's current version is a person's choice and is changing
     (``save_origin`` says ``copy_choice``, ADR-P05 D7): that choice again, origin
     ``copy_choice``, whose diff review goes to the person, but only when every code they
     chose is still offered and no code was added; otherwise nothing, and the person
     chooses again from the options as they are now;
   - when the declared options are empty: an empty choice, recorded with nothing asked
     (there is nothing to choose).

2. :func:`chosen_by_person`, the completion check: the current version is not changing
   (invalidated or dirty), was saved from an answered choice of this instance and holds
   exactly the chosen codes, or it is a copied choice (current once the person accepted
   its review, or pending a review that is still open), or the options were empty and
   the recorded choice is empty. A stale newest answer fails with :data:`STALE`; anything
   else fails with what to do.

No agent word appears here: the kind, the field and the options are the declaration's.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aksha.runtime.artifacts.types import ArtifactVersion, Origin, WriterId
from aksha.runtime.artifacts.versions import current, unaccepted_revisions, versions
from aksha.runtime.context.reads import declared_versions
from aksha.runtime.interrupts.reducers import INTERRUPT_RAISED, INTERRUPT_RESOLVED
from aksha.runtime.interrupts.types import AnswerSave, DeclaredInterrupt
from aksha.runtime.invalidation.reviews import open_reviews
from aksha.runtime.invalidation.saving import save_origin
from aksha.runtime.invalidation.types import STALE_STATES
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import CheckResult
from aksha.runtime.tasks.view import TaskView

ANSWER_ONLY = "answer_only"
"""The code of a tool's save of a kind a declared choice saves: only the answer saves it."""

NOTHING_ASKED = ""
"""The ``answer`` of an empty choice the runtime recorded because there was nothing to ask."""

ASK_FIRST = (
    "the person chooses here, not you: call ask with a one-line summary of the options for "
    "the question; the options and the save are the runtime's"
)


STALE = (
    "the options changed after the person chose (what they were drawn from has a newer "
    "version): ask again with a one-line summary; the runtime lists the options as they are now"
)
"""The check's message when the person's newest answer chose from options that changed."""

ANSWER_REFUSED = "answer_save_refused"
"""The reason prefix of an instance ended because the runtime's save of an answer was refused."""

ANSWER_SUB = "derived"
"""The sub-step of the turn's first call position the runtime's answer save is recorded under."""


@dataclass(frozen=True)
class AnswerSavePlan:
    """What the runtime saves from a choice: the kind, the codes, the origin, the answer."""

    kind: str
    key: str | None
    chosen: tuple[str, ...]
    origin: Origin
    answer: str


def answer_kinds(tree: CompiledTree) -> frozenset[str]:
    """The kinds (full names) a declared choice of ``tree`` saves: no tool may save them."""
    return frozenset(
        tree.kind(n.task.interrupt.saves.kind)
        for n in tree.walk()
        if n.task.interrupt is not None and n.task.interrupt.saves is not None
    )


def declared_saves(view: TaskView) -> tuple[DeclaredInterrupt, AnswerSave] | None:
    """The instance's declared choice and what it saves, or None when it saves nothing."""
    declared = view.tree.node(view.instance.task_path).task.interrupt
    if declared is None or declared.saves is None:
        return None
    return declared, declared.saves


def option_codes(view: TaskView) -> tuple[str, ...]:
    """The codes the declared options give now (none when the Task declares none)."""
    declared = view.tree.node(view.instance.task_path).task.interrupt
    if declared is None or declared.options is None:
        return ()
    return tuple(str(c) for c, _ in declared.options(view))


@dataclass(frozen=True)
class Answered:
    """A resolved choice of this instance's writer: what was offered, and what was chosen.

    ``options`` and ``drawn_from`` are the raise's: the codes offered and the refs of the
    Task's declared reads the options were built from (absent on a raise that predates
    them). ``seq`` is the resolution's sequence number.
    """

    interrupt: str
    chosen: tuple[str, ...]
    options: tuple[str, ...]
    drawn_from: tuple[str, ...] | None
    seq: int


def _writer_json(view: TaskView) -> Mapping[str, object]:
    return WriterId(view.tree.agent, view.instance.task_path, view.instance.key).as_json()


def answered_choices(view: TaskView) -> tuple[Answered, ...]:
    """Each resolved choice raised for this instance, in resolution order.

    A choice counts when this instance raised it (its run and instance), or when it names
    this instance's writer key (``writer``), whichever run raised it: the fresh choice a
    rejected review raises is the writer's, and a later run of the writer saves from it.
    """
    writer = _writer_json(view)
    raised: dict[str, Mapping[str, Any]] = {}
    out: list[Answered] = []
    for e in view.log.events:
        p = e.payload
        if e.type == INTERRUPT_RAISED and p.get("kind") == "choice":
            mine = e.run == view.instance.run and e.instance == view.instance.instance
            if mine or p.get("writer") == writer:
                raised[str(p.get("interrupt"))] = p
        elif e.type == INTERRUPT_RESOLVED and str(p.get("interrupt")) in raised:
            asked = raised[str(p["interrupt"])]
            chosen = p.get("chosen")
            if chosen is None:  # resolved before ``chosen`` was recorded: the answers
                chosen = list(dict.fromkeys(dict(p.get("answers") or {}).values()))
            drawn = asked.get("drawn_from")
            out.append(Answered(
                str(p["interrupt"]), tuple(str(c) for c in chosen),
                tuple(str(o) for o in asked.get("options", ())),
                None if drawn is None else tuple(str(d) for d in drawn), e.seq,
            ))  # fmt: skip
    return tuple(out)


def resolved_choices(view: TaskView) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """``(interrupt, chosen codes)`` for each resolved choice of this instance, in order."""
    return tuple((a.interrupt, a.chosen) for a in answered_choices(view))


def current_reads(view: TaskView) -> tuple[str, ...]:
    """The refs of the Task's declared reads at their current versions: what options use."""
    return tuple(str(v.ref) for v in declared_versions(view.log, view.tree, view.instance))


def is_stale(view: TaskView, answered: Answered) -> bool:
    """Whether the options ``answered`` chose from are no longer the options now.

    A read the options were drawn from has a newer current version, or the codes offered
    now differ from the codes the person chose from: the answer is not saved, and the
    person is asked again with the options as they are now.
    """
    if answered.drawn_from is not None and not set(answered.drawn_from) <= set(current_reads(view)):
        return True
    return answered.options != option_codes(view)


def stale_choice(view: TaskView) -> Answered | None:
    """The newest resolved choice when it is stale and no version names it yet, else None."""
    answered = answered_choices(view)
    if not answered:
        return None
    newest = answered[-1]
    found = declared_saves(view)
    if found is None:
        return None
    if newest.interrupt in _named_answers(view, view.tree.kind(found[1].kind)):
        return None
    return newest if is_stale(view, newest) else None


def _refused_since(view: TaskView, seq: int) -> bool:
    """Whether a runtime answer save of this instance was refused after ``seq``."""
    run, instance = view.instance.run, view.instance.instance
    prefix = f"{run}/{instance}/t"
    for e in view.log.events:
        if (
            e.seq > seq
            and e.type == "step.completed"
            and e.run == run
            and e.instance == instance
            and e.payload.get("outcome") == "error"
            and str(e.step or "").startswith(prefix)
            and str(e.step or "").endswith(f"/c01/{ANSWER_SUB}")
        ):
            return True
    return False


def refusal_text(code: str | None, value: Mapping[str, Any] | None) -> str:
    """``answer_save_refused (<code>): <message>`` from a refused answer step's record."""
    shown = (value or {}).get("view")
    text = str(shown.get("text") or "") if isinstance(shown, Mapping) else ""
    try:
        parsed = json.loads(text.partition("\n")[0])
        message = str(parsed.get("message") or "") if isinstance(parsed, dict) else text
    except ValueError:
        message = text
    return f"{ANSWER_REFUSED} ({code or 'error'}): {message}".rstrip(": ")


def refused_answer(log: LogView, run: str, instance: str) -> str | None:
    """Why the runtime's latest answer save of ``instance`` was refused, or None."""
    found: str | None = None
    for e in log.events:
        if (
            e.type == "step.completed"
            and e.run == run
            and e.instance == instance
            and str(e.step or "").endswith(f"/c01/{ANSWER_SUB}")
        ):
            p = e.payload
            ok = p.get("outcome") == "ok"
            found = None if ok else refusal_text(p.get("code"), p.get("value"))
    return found


def _named_answers(view: TaskView, kind: str) -> frozenset[str]:
    return frozenset(
        str(e.payload["answer"])
        for e in view.log.events
        if e.type == "artifact.saved" and e.payload.get("kind") == kind and "answer" in e.payload
    )


def _saved_since_entry(view: TaskView, kind: str) -> bool:
    saved = False
    for e in view.log.events:
        if e.run != view.instance.run or e.instance != view.instance.instance:
            continue
        if e.type == "task.entered":
            saved = False
        elif e.type == "artifact.saved" and e.payload.get("kind") == kind:
            saved = True
    return saved


def _offered(view: TaskView, interrupt: str) -> tuple[str, ...] | None:
    """The codes the choice ``interrupt`` offered, or None when no such choice was raised."""
    for e in view.log.events:
        p = e.payload
        if e.type == INTERRUPT_RAISED and str(p.get("interrupt")) == interrupt:
            return tuple(str(o) for o in p.get("options", ()))
    return None


def answer_save_plan(view: TaskView) -> AnswerSavePlan | None:
    """What the runtime saves from this instance's choice now, or None (module doc, item 1)."""
    found = declared_saves(view)
    if found is None:
        return None
    _, saves = found
    kind = view.tree.kind(saves.kind)
    key = view.key
    answered = answered_choices(view)
    if answered and answered[-1].interrupt not in _named_answers(view, kind):
        newest = answered[-1]
        if is_stale(view, newest) or _refused_since(view, newest.seq):
            return None  # asked again with the options as they are now, or already refused
        return AnswerSavePlan(kind, key, newest.chosen, "choice", newest.interrupt)
    if _saved_since_entry(view, kind):
        return None
    codes = option_codes(view)
    old = current(view.log, kind, key)
    if old is not None and save_origin(view.log, kind, key) == "copy_choice":
        before = {str(b) for b in old.content.get(saves.field) or ()}
        offered = None if not old.answer else _offered(view, old.answer)
        if old.answer == NOTHING_ASKED:
            offered = ()
        # Copied only when every code the person chose is still offered and no new code is
        # offered: otherwise the person chooses again, from the options as they are now.
        if offered is not None and before <= set(codes) and set(codes) <= set(offered):
            kept = tuple(c for c in codes if c in before)
            return AnswerSavePlan(kind, key, kept, "copy_choice", old.answer or NOTHING_ASKED)
        return None
    if not codes:
        return AnswerSavePlan(kind, key, (), "task", NOTHING_ASKED)
    return None


def answer_content(view: TaskView, plan: AnswerSavePlan) -> Mapping[str, Any]:
    """The content the plan saves: the declaration's ``build``, its field set to the codes."""
    found = declared_saves(view)
    assert found is not None
    _, saves = found
    return {**saves.build(view, plan.chosen), saves.field: list(plan.chosen)}


def _pending_copy(view: TaskView, kind: str) -> ArtifactVersion | None:
    """This instance's copied choice whose diff review is open (a rejected one is not)."""
    pending = unaccepted_revisions(view.log)
    reviewed = {str(r.revision) for r in open_reviews(view.log).values()}
    for v in reversed(versions(view.log, kind, view.key)):
        if (
            str(v.ref) in pending
            and str(v.ref) in reviewed
            and v.origin == "copy_choice"
            and v.run == view.instance.run
            and v.writer.task_path == view.instance.task_path
        ):
            return v
    return None


def _codes(value: object) -> list[str] | None:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return None
    return [str(v) for v in value]


def chosen_by_person(view: TaskView) -> CheckResult:
    """The Task's choice artifact holds what the person chose (module doc, item 2)."""
    found = declared_saves(view)
    if found is None:
        return CheckResult(False, "this Task declares no choice whose answer it saves")
    _, saves = found
    kind = view.tree.kind(saves.kind)
    if _pending_copy(view, kind) is not None:
        return CheckResult(True)  # the person reviews the copied choice; readers wait for it
    v = view.own(saves.kind)
    if v is None or v.writer.task_path != view.instance.task_path:
        return CheckResult(False, STALE if stale_choice(view) is not None else ASK_FIRST)
    state = view.freshness(str(v.ref))
    if state in STALE_STATES:  # what the person chose from moved: the runtime copies it
        return CheckResult(False, "the person's choice rests on work that changed: it goes "
                                  "back to the person; finish once it is saved")  # fmt: skip
    got = _codes(v.content.get(saves.field))
    stale = stale_choice(view)
    if stale is not None:
        return CheckResult(False, STALE)
    if v.origin == "copy_choice":
        return CheckResult(got is not None, f"{saves.field} lists the chosen codes")
    answered = dict(resolved_choices(view))
    if v.origin == "choice" and v.answer in answered:
        chosen = list(answered[v.answer])
        message = f"{saves.field} must be exactly the codes the person chose: {chosen}"
        return CheckResult(got == chosen, message)
    if v.answer == NOTHING_ASKED and v.run == view.instance.run and not option_codes(view):
        return CheckResult(got == [], f"there was nothing to choose: {saves.field} is empty")
    return CheckResult(False, ASK_FIRST)
