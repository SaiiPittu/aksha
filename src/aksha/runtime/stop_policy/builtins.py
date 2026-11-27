"""The five opt-in stop policies (ADR-P16 D4): goal, plan, forced tool, todo reminder, recite.

An agent opts into one with ``Goal(check)``, ``Plan(kind, accept)``, ``ForcedTool(tool,
when)`` or ``TodoReminder(every)`` on a Task (or agent-wide); it never subclasses one.
Each sits inside its Task's policy on the stack, so it judges a candidate before the Task
does, and its CONTINUE passes the same limits (a goal's veto counts toward the stop-veto
limit like the Task's own).

==============  ===================================  =================  ==================
Policy          Judges                               Verdicts           Adds to the request
==============  ===================================  =================  ==================
goal            a finish while the goal check fails  CONTINUE, PASS     one line naming
                (a raising check is FAIL)            (FAIL)             the goal
plan            a turn or a finish before a plan     CONTINUE, PASS     "plan first"; tools
                artifact passes its accept rule                         narrowed to the save
forced tool     nothing                              PASS               while ``when``
                                                                        holds: one tool and
                                                                        a ``tool_choice``
todo reminder   nothing                              PASS               every N turns, the
                                                                        open todos
recite          nothing                              PASS               every turn, a pure
                                                                        projection, capped,
                                                                        as data
==============  ===================================  =================  ==================

Every check is the agent's pure function over a ``TaskView``, run through ``run_check``.
"""

from __future__ import annotations

from dataclasses import dataclass

from aksha.runtime.budgets.limits import turns_used
from aksha.runtime.stop_policy.types import (
    PASS,
    Prep,
    Render,
    StopCandidate,
    StopPolicyOptIn,
    StopView,
    Verdict,
)
from aksha.runtime.tasks.checks import check_name, run_check
from aksha.runtime.tasks.completion import evaluate_in, evaluated_fact
from aksha.runtime.tasks.status import run_state
from aksha.runtime.tasks.types import Check, CheckResult, Evaluation, EvaluationCode, InstanceRef
from aksha.runtime.tasks.view import TaskView

SAVE_TOOL = "save_artifact"
"""The one tool plan mode keeps (with ``finish`` and ``ask``, which the gate always keeps)."""


def _view(view: StopView, ref: InstanceRef) -> TaskView:
    return TaskView(view.log, view.tree, ref)


def _evaluated(ref: InstanceRef, check: Check, result: CheckResult, raised: bool) -> Evaluation:
    code: EvaluationCode = "" if result.ok else ("check_raised" if raised else "check_failed")
    return Evaluation(
        ref.instance, result.ok, result.message, result.count, tuple(result.keys[:5]),
        result.clears, code, (), check_name(check),
    )  # fmt: skip


@dataclass(frozen=True)
class GoalPolicy:
    """Goal condition: vetoes a finish while ``check`` fails."""

    instance: InstanceRef
    check: Check

    @property
    def ref(self) -> str:
        """``goal:fix``."""
        return f"goal:{self.instance.instance}"

    def prepare(self, view: StopView) -> Prep:
        """One line naming the goal."""
        return Prep(brief=(f"Goal: {check_name(self.check)} must pass before you finish.",))

    def judge(self, view: StopView, c: StopCandidate) -> Verdict:
        """On a finish: CONTINUE with the check's message while it fails, else PASS."""
        if c.kind != "finish":
            return PASS
        result, raised = run_check(self.check, _view(view, self.instance))
        if result.ok:
            return PASS
        fact = evaluated_fact(
            self.instance, _evaluated(self.instance, self.check, result, raised), on="finish"
        )
        if raised:
            return Verdict("fail", result.message, facts=(fact,), code="check_raised")
        message = result.message or f"goal {check_name(self.check)} is not met"
        return Verdict("continue", message, facts=(fact,))

    def limit(self, view: StopView, c: StopCandidate, proposed: Verdict) -> Verdict | None:
        """The goal sets no limit; the Task's stop-veto limit counts its vetoes."""
        return None


@dataclass(frozen=True)
class PlanPolicy:
    """Plan mode: until a ``kind`` artifact passes ``accept``, only the plan save is offered."""

    instance: InstanceRef
    kind: str
    accept: Check

    @property
    def ref(self) -> str:
        """``plan:fix``."""
        return f"plan:{self.instance.instance}"

    def accepted(self, view: StopView) -> bool:
        """Whether this instance's plan exists and passes the accept rule."""
        tv = _view(view, self.instance)
        if tv.own(self.kind) is None:
            return False
        result, _ = run_check(self.accept, tv)
        return result.ok

    def _reason(self) -> str:
        rule = check_name(self.accept)
        return f"Write the plan first: save a {self.kind} artifact that passes {rule}."

    def prepare(self, view: StopView) -> Prep:
        """While the plan is not accepted: the plan line, and tools narrowed to the save."""
        if self.accepted(view):
            return Prep()
        return Prep(brief=(self._reason(),), tools=frozenset({SAVE_TOOL}), narrowed_by=self.ref)

    def judge(self, view: StopView, c: StopCandidate) -> Verdict:
        """A turn or a finish before the plan is accepted: CONTINUE with the plan line."""
        if c.kind not in ("tools", "finish") or self.accepted(view):
            return PASS
        return Verdict("continue", self._reason())

    def limit(self, view: StopView, c: StopCandidate, proposed: Verdict) -> Verdict | None:
        """Plan mode sets no limit."""
        return None


@dataclass(frozen=True)
class ForcedToolPolicy:
    """Forced tool choice: while ``when`` holds, the next turn is offered ``tool`` only."""

    instance: InstanceRef
    tool: str
    when: Check

    @property
    def ref(self) -> str:
        """``forced_tool:fix``."""
        return f"forced_tool:{self.instance.instance}"

    def prepare(self, view: StopView) -> Prep:
        """While ``when`` holds: one tool, one line saying so, and the ``tool_choice`` hint."""
        result, _ = run_check(self.when, _view(view, self.instance))
        if not result.ok:
            return Prep()
        line = f"Call {self.tool} now; it is the only tool offered this turn."
        return Prep(
            brief=(line,),
            tools=frozenset({self.tool}),
            narrowed_by=self.ref,
            tool_choice=self.tool,
        )

    def judge(self, view: StopView, c: StopCandidate) -> Verdict:
        """Never decides: the narrowing is the whole effect."""
        return PASS

    def limit(self, view: StopView, c: StopCandidate, proposed: Verdict) -> Verdict | None:
        """A forced tool sets no limit."""
        return None


@dataclass(frozen=True)
class TodoReminderPolicy:
    """Todo reminder: every ``every`` turns, the instance's open todos; never vetoes."""

    instance: InstanceRef
    every: int

    @property
    def ref(self) -> str:
        """``todo_reminder:fix``."""
        return f"todo_reminder:{self.instance.instance}"

    def todos(self, view: StopView) -> tuple[str, ...]:
        """The unmet checks: each unsettled child's, or a leaf's own failing check."""
        ref = self.instance
        state = run_state(view.log, view.tree, ref.run)
        node = view.tree.node(ref.task_path)
        if node.is_leaf:
            own = evaluate_in(state, ref)
            return () if own.ok else (own.message or "finish the work",)
        out = []
        for cref in state.children(ref):
            if state.settled(cref):
                continue
            evaluation = evaluate_in(state, cref)
            if not evaluation.ok:
                out.append(f"{cref.instance}: {evaluation.message or 'not done'}")
        return tuple(out)

    def prepare(self, view: StopView) -> Prep:
        """On every ``every``-th turn of the instance, one line listing the open todos."""
        turns = turns_used(view.log, self.instance.run, self.instance.instance)
        if self.every < 1 or turns == 0 or turns % self.every:
            return Prep()
        todos = self.todos(view)
        return Prep(brief=("Open todos: " + "; ".join(todos),)) if todos else Prep()

    def judge(self, view: StopView, c: StopCandidate) -> Verdict:
        """Never vetoes."""
        return PASS

    def limit(self, view: StopView, c: StopCandidate, proposed: Verdict) -> Verdict | None:
        """A reminder sets no limit."""
        return None


RECITE_CUT = "[recitation cut at {n} characters]"


@dataclass(frozen=True)
class RecitePolicy:
    """Recitation: every turn, a pure projection of the log, capped, placed as data."""

    instance: InstanceRef
    render: Render
    max_chars: int

    @property
    def ref(self) -> str:
        """``recite:lead``."""
        return f"recite:{self.instance.instance}"

    def lines(self, view: StopView) -> tuple[str, ...]:
        """The render's lines; a render that raises recites one line naming the error."""
        try:
            found = self.render(_view(view, self.instance))
        except Exception as exc:  # a broken render never widens or crashes a turn
            return (f"{self.ref} failed: {type(exc).__name__}",)
        if isinstance(found, str):
            return (found,)
        return tuple(str(x) for x in found)

    def prepare(self, view: StopView) -> Prep:
        """The lines joined, cut at ``max_chars`` with a line saying so."""
        text = "\n".join(self.lines(view))
        if not text:
            return Prep()
        if len(text) > self.max_chars:
            note = RECITE_CUT.format(n=self.max_chars)
            text = text[: max(0, self.max_chars - len(note) - 1)] + "\n" + note
        return Prep(data=(text,))

    def judge(self, view: StopView, c: StopCandidate) -> Verdict:
        """Never vetoes."""
        return PASS

    def limit(self, view: StopView, c: StopCandidate, proposed: Verdict) -> Verdict | None:
        """A recitation sets no limit."""
        return None


BuiltinPolicy = GoalPolicy | PlanPolicy | ForcedToolPolicy | TodoReminderPolicy | RecitePolicy


def build(opt: StopPolicyOptIn, instance: InstanceRef) -> BuiltinPolicy:
    """The policy an opt-in declares, for one instance. The loader refused unknown names."""
    p = opt.params
    if opt.name == "goal":
        return GoalPolicy(instance, p["check"])  # type: ignore[arg-type]
    if opt.name == "plan":
        return PlanPolicy(instance, str(p["kind"]), p["accept"])  # type: ignore[arg-type]
    if opt.name == "forced_tool":
        return ForcedToolPolicy(instance, str(p["tool"]), p["when"])  # type: ignore[arg-type]
    if opt.name == "recite":
        return RecitePolicy(instance, p["render"], int(p["max_chars"]))  # type: ignore[arg-type,call-overload]
    return TodoReminderPolicy(instance, int(p["every"]))  # type: ignore[call-overload]
