"""A person's choice as an artifact (ADR-P04 choice binding, ADR-P17 D2): the plan and the check.

A Task that declares ``DeclaredInterrupt("choice", ..., options=..., saves=AnswerSave(...))``
does not let the model decide. The options are built by code from the Task's view; the
model may only ask, with a one-line summary; the person answers; and the runtime, not the
model, saves the kind from the answer (``loop/answers.py``). This module
holds the two pure halves:

1. :func:`answer_save_plan`: what the runtime saves at the start of the instance's next
   turn, if anything. In order:

   - the newest resolved choice of this instance that no version names yet: the codes the
     person chose, origin ``choice``;
   - nothing more once the instance saved since it was last entered;
   - in a rebuild, when the slot's current version is a person's choice and is changing
     (``save_origin`` says ``copy_choice``, ADR-P05 D7): that choice again, narrowed to the
     codes still offered, origin ``copy_choice``, whose diff review goes to the person;
   - when the declared options are empty and the choice is ``multiple``: an empty choice,
     recorded with nothing asked (there is nothing to choose).

2. :func:`chosen_by_person`, the completion check: the current version is not changing
   (invalidated or dirty), was saved from an answered choice of this instance and holds
   exactly the chosen codes, or it is a copied
   choice (current once the person accepted its review, or pending that review), or the
   options were empty and the recorded choice is empty. Anything else fails with what to do.

No agent word appears here: the kind, the field and the options are the declaration's.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aksha.runtime.artifacts.types import ArtifactVersion, Origin
from aksha.runtime.artifacts.versions import current, unaccepted_revisions, versions
from aksha.runtime.interrupts.reducers import INTERRUPT_RAISED, INTERRUPT_RESOLVED
from aksha.runtime.interrupts.types import AnswerSave, DeclaredInterrupt
from aksha.runtime.invalidation.saving import save_origin
from aksha.runtime.invalidation.types import STALE_STATES
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


def resolved_choices(view: TaskView) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """``(interrupt, chosen codes)`` for each resolved choice raised on this instance, in order."""
    mine: set[str] = set()
    out: list[tuple[str, tuple[str, ...]]] = []
    for e in view.log.events:
        if e.run != view.instance.run:
            continue
        p = e.payload
        if e.type == INTERRUPT_RAISED and e.instance == view.instance.instance:
            if p.get("kind") == "choice":
                mine.add(str(p.get("interrupt")))
        elif e.type == INTERRUPT_RESOLVED and str(p.get("interrupt")) in mine:
            chosen = p.get("chosen")
            if chosen is None:  # resolved before ``chosen`` was recorded: the answers
                chosen = list(dict.fromkeys(dict(p.get("answers") or {}).values()))
            out.append((str(p["interrupt"]), tuple(str(c) for c in chosen)))
    return tuple(out)


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


def answer_save_plan(view: TaskView) -> AnswerSavePlan | None:
    """What the runtime saves from this instance's choice now, or None (module doc, item 1)."""
    found = declared_saves(view)
    if found is None:
        return None
    declared, saves = found
    kind = view.tree.kind(saves.kind)
    key = view.key
    answered = resolved_choices(view)
    if answered and answered[-1][0] not in _named_answers(view, kind):
        ident, chosen = answered[-1]
        return AnswerSavePlan(kind, key, chosen, "choice", ident)
    if _saved_since_entry(view, kind):
        return None
    codes = option_codes(view)
    old = current(view.log, kind, key)
    if old is not None and save_origin(view.log, kind, key) == "copy_choice":
        before = old.content.get(saves.field) or ()
        kept = tuple(c for c in codes if c in {str(b) for b in before})
        return AnswerSavePlan(kind, key, kept, "copy_choice", old.answer or NOTHING_ASKED)
    if declared.multiple and not codes:
        return AnswerSavePlan(kind, key, (), "task", NOTHING_ASKED)
    return None


def answer_content(view: TaskView, plan: AnswerSavePlan) -> Mapping[str, Any]:
    """The content the plan saves: the declaration's ``build``, its field set to the codes."""
    found = declared_saves(view)
    assert found is not None
    _, saves = found
    return {**saves.build(view, plan.chosen), saves.field: list(plan.chosen)}


def _pending_copy(view: TaskView, kind: str) -> ArtifactVersion | None:
    pending = unaccepted_revisions(view.log)
    for v in reversed(versions(view.log, kind, view.key)):
        if (
            str(v.ref) in pending
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
        return CheckResult(False, ASK_FIRST)
    state = view.freshness(str(v.ref))
    if state in STALE_STATES:  # what the person chose from moved: the runtime copies it
        return CheckResult(False, "the person's choice rests on work that changed: it goes "
                                  "back to the person; finish once it is saved")  # fmt: skip
    got = _codes(v.content.get(saves.field))
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
