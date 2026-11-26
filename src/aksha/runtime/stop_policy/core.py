"""The three stop policies always on the stack (ADR-P16 D4): Run, Task and interrupt.

=============  ==========================================================================
Policy         Judges
=============  ==========================================================================
Run            outermost, one per open run. Relays the scheduler's pick (PUSH), holds when
               nothing may run but a person can unblock it, and is the only policy that
               returns STOP: ``complete``, ``partial`` (explicit unknowns, ADR-P24 D6),
               ``blocked`` (naming the blocked instances) or ``failed`` on a run fault. Its
               limit replaces a CONTINUE with the budget interrupt once a budget dimension
               is spent (ADR-P14), or, once the run is finalizing, ends it ``partial``
               when the reserve is spent too (ADR-P24 D5); it ends a resumed run at the
               restart limit.
Task           one per Task instance on the stack. A leaf: entry (DONE when its check
               passes on entry, so untouched work takes no turn), CONTINUE after tools, and
               on a finish its completion check (DONE, a veto with the check's message, or
               FAIL at once for ``check_raised`` and ``child_blocked``). A parent takes no
               turns: it is DONE once its children settle and its check passes. A
               supervisor follows the supervisor rule (ADR-P15 D7). A re-entry of a leaf
               records its re-entry mode, resume or fresh (decision 36). Its limit fails the
               instance at the turn limit and at the stop-veto limit (the retry cap: the
               ``max_finish_vetoes``-th refused finish since entry is FAIL). A finish
               while a person's follow-up or steer for the instance is pending is a
               CONTINUE whose reason is the person's text (ADR-P27 D4), before the
               completion check, and no veto: the stop-veto limit does not count it.
               A supervisor whose check passes with such input pending takes one more
               turn the same way, instead of finishing.
interrupt      one per open interrupt, innermost: HOLD until a person closes it.
=============  ==========================================================================

Every policy is a frozen dataclass and reads only the ``StopView`` and the candidate.
"""

from __future__ import annotations

from dataclasses import dataclass

from aksha.runtime.budgets.limits import limit_reached, turns_used, vetoes_since_entry
from aksha.runtime.context.reentry import choose_reentry
from aksha.runtime.context.types import effective_policy
from aksha.runtime.finalization.state import finalizing
from aksha.runtime.inputs.reducers import pending_input
from aksha.runtime.inputs.types import QueuedInput
from aksha.runtime.interrupts.reducers import pending_interrupts
from aksha.runtime.stop_policy import hold as holds
from aksha.runtime.stop_policy.types import (
    PASS,
    InterruptRequest,
    Prep,
    StopCandidate,
    StopView,
    Verdict,
)
from aksha.runtime.tasks.completion import evaluate_in, evaluated_fact
from aksha.runtime.tasks.rebuilds import rebuild_targets
from aksha.runtime.tasks.scheduler import (
    enter,
    reentry,
    run_outcome,
    runnable_below,
    runnable_in,
    withdrawals,
    writer_of,
)
from aksha.runtime.tasks.status import RunState, run_state
from aksha.runtime.tasks.supervisor import is_supervisor, supervisor_step
from aksha.runtime.tasks.types import SETTLED, InstanceRef

BUDGET_REACHED = "budget.reached"
MALFORMED_REASON = "Your last reply could not be read; answer again."
FAULT_REASON = "fault"
OVERFLOW_REASON = "context_overflow"


def run_ref(run: str) -> str:
    """The Run policy's ID."""
    return f"run:{run}"


def task_ref(ref: InstanceRef) -> str:
    """A Task policy's ID."""
    return f"task:{ref.instance}"


PERSON_INPUT = "person_input"
"""The code of a CONTINUE a person's pending follow-up or steer turned a finish into."""


def person_reason(waiting: tuple[QueuedInput, ...]) -> str:
    """Why a finish waits for a person's pending input: their words, in queue order."""
    parts = [
        f"the person added a follow-up:\n{i.text}" if i.lane == "follow_up"
        else "the person sent a message, shown above"
        for i in waiting
    ]  # fmt: skip
    return "; ".join(parts)


@dataclass(frozen=True)
class RunPolicy:
    """The root of the stack (module doc)."""

    run: str

    @property
    def ref(self) -> str:
        """``run:R-0001``."""
        return run_ref(self.run)

    def prepare(self, view: StopView) -> Prep:
        """The run adds nothing to a request."""
        return Prep()

    def judge(self, view: StopView, c: StopCandidate) -> Verdict:
        """Relay the pick, hold for a person, or end the run."""
        if c.kind == "fault" and c.instance is None:
            return Verdict("stop", c.reason or FAULT_REASON, outcome="failed", code="fault")
        # A key that left its source is withdrawn whatever its facts say, so the pick
        # below is right before the withdrawal facts commit with this decision.
        facts = withdrawals(view.log, view.tree, self.run)
        state = run_state(view.log, view.tree, self.run)
        for pick in runnable_in(state)[: view.tree.concurrency]:
            if holds.hold_reason(view.log, pick) is None:
                return Verdict("push", child=pick, facts=facts)
        outcome = run_outcome(view.log, view.tree, self.run)
        if outcome is None:
            waiting = pending_interrupts(view.log, self.run)
            reason = "waiting on a person: " + ", ".join(waiting) if waiting else "waiting"
            return Verdict("hold", reason, code="interrupt_open", facts=facts)
        if outcome.outcome == "complete":
            return Verdict("stop", "completion_checks_passed", outcome="complete", facts=facts)
        if outcome.outcome == "partial":
            reason = f"partial: {len(outcome.unknowns)} unknown"
            return Verdict("stop", reason, outcome="partial", facts=facts)
        blocked = ", ".join(outcome.blockers)
        reason = f"blocked: {blocked}" if blocked else "blocked"
        return Verdict("stop", reason, outcome="blocked", facts=facts)

    def limit(self, view: StopView, c: StopCandidate, proposed: Verdict) -> Verdict | None:
        """The budget first (a spent dimension raises the budget interrupt), then restarts."""
        if c.kind == "resume" or proposed.resumes_turn:
            for hit in limit_reached(view.log, self.run, None, view.limits):
                counts = (("resumes", hit.used), ("allowed", hit.allowed))
                return Verdict("stop", hit.limit, outcome="failed", code=hit.limit, counts=counts)
            return None
        m = view.meter
        if m is None:
            return None
        spent = m.exhausted()
        if not spent:
            return None
        d = spent[0]
        used, allowed = m.used[d], m.allowed[d]
        counts = ((d, used), ("allowed", allowed))
        if finalizing(view.log, self.run) is not None:
            reason = f"reserve spent: {d} {used} of {allowed}"
            return Verdict("stop", reason, outcome="partial", code="reserve_spent", counts=counts)
        n = 1 + sum(1 for e in view.log.events if e.run == self.run and e.type == BUDGET_REACHED)
        raises = InterruptRequest(f"budget-{n}", d, used, allowed)
        reason = f"budget {d} spent: {used} of {allowed}; grant more or finalize"
        return Verdict("hold", reason, raises=raises, code=d, counts=counts)


@dataclass(frozen=True)
class InterruptPolicy:
    """An open interrupt: HOLD until a person answers it or the runtime voids it."""

    interrupt: str
    run: str
    instance: str | None = None

    @property
    def ref(self) -> str:
        """``interrupt:I-0003``."""
        return f"interrupt:{self.interrupt}"

    def prepare(self, view: StopView) -> Prep:
        """Nothing: a held instance takes no turn."""
        return Prep()

    def judge(self, view: StopView, c: StopCandidate) -> Verdict:
        """HOLD, naming the interrupt."""
        return Verdict("hold", f"waiting on {self.interrupt}", code="interrupt_open")

    def limit(self, view: StopView, c: StopCandidate, proposed: Verdict) -> Verdict | None:
        """An interrupt never limits."""
        return None


@dataclass(frozen=True)
class TaskPolicy:
    """One Task instance (module doc)."""

    instance: InstanceRef

    @property
    def ref(self) -> str:
        """``task:extract[C1]``."""
        return task_ref(self.instance)

    def prepare(self, view: StopView) -> Prep:
        """The Task's own brief, if it declares one."""
        brief = view.tree.node(self.instance.task_path).task.brief
        return Prep(brief=(brief,) if brief else ())

    def judge(self, view: StopView, c: StopCandidate) -> Verdict:
        """Entry, turn ends and finish requests of this instance (module doc)."""
        ref = self.instance
        node = view.tree.node(ref.task_path)
        state = run_state(view.log, view.tree, ref.run)
        status = state.status(ref)
        entering = c.kind == "enter" and status in ("new", "done", "blocked")
        if entering and ref in rebuild_targets(view.log, view.tree, ref.run):  # P05 D7, D8
            choice = None
            if node.is_leaf:  # decision 36: the mode is chosen once, here, and recorded
                policy = effective_policy(view.context_policy)
                found = choose_reentry(view.log, view.tree, ref, policy, blobs=view.blobs)
                choice = found.as_json()
            writer = writer_of(view.tree, ref)
            facts = reentry(view.log, view.tree, ref.run, writer, choice=choice)
            if node.is_leaf:
                return Verdict("continue", node.task.brief or "", facts=facts)
            return Verdict("pass", facts=facts)
        if status in SETTLED:
            return PASS
        if c.kind == "enter" and status == "new":
            entry = enter(view.log, view.tree, ref, cause="scheduler")
            if entry.done:
                return Verdict("done", facts=entry.facts)
            if node.is_leaf:
                return Verdict("continue", node.task.brief or "", facts=entry.facts)
            return Verdict("pass", facts=entry.facts)  # its children are picked next
        if node.is_leaf:
            return self._leaf(view, c, state)
        if is_supervisor(node.task):
            return self._supervisor(view, c, state)
        return self._parent(state)

    def _leaf(self, view: StopView, c: StopCandidate, state: RunState) -> Verdict:
        ref = self.instance
        waiting = pending_input(view.log, ref) if c.kind == "finish" else ()
        if waiting:
            return Verdict("continue", person_reason(waiting), code=PERSON_INPUT)
        if c.kind == "finish":
            evaluation = evaluate_in(state, ref)
            fact = (evaluated_fact(ref, evaluation, on="finish"),)
            if evaluation.ok:
                return Verdict("done", facts=fact)
            if evaluation.code in ("check_raised", "child_blocked"):
                return Verdict("fail", evaluation.message, facts=fact, code=evaluation.code)
            return Verdict("continue", evaluation.message, facts=fact)
        if c.kind == "fault":
            return Verdict("fail", c.reason or FAULT_REASON, code="fault")
        if c.kind == "overflow":
            return Verdict("fail", OVERFLOW_REASON, code=OVERFLOW_REASON)
        if c.kind == "malformed":
            return Verdict("continue", MALFORMED_REASON)
        if c.kind == "resume":
            return Verdict("continue", resumes_turn=True)
        return Verdict("continue")

    def _parent(self, state: RunState) -> Verdict:
        ref = self.instance
        evaluation = evaluate_in(state, ref)
        if evaluation.code == "children_pending":
            return PASS
        fact = (evaluated_fact(ref, evaluation, on="parent"),)
        if evaluation.ok:
            return Verdict("done", facts=fact)
        return Verdict("fail", evaluation.message, facts=fact, code=evaluation.code)

    def _supervisor(self, view: StopView, c: StopCandidate, state: RunState) -> Verdict:
        ref = self.instance
        if c.kind in ("tools", "finish", "malformed"):
            evaluation = evaluate_in(state, ref)
            waiting = pending_input(view.log, ref) if evaluation.ok else ()
            if waiting:  # ADR-P27 B4: a supervisor takes turns, so it reads the input
                return Verdict("continue", person_reason(waiting), code=PERSON_INPUT)
            if evaluation.ok:
                return Verdict("done", facts=(evaluated_fact(ref, evaluation, on="parent"),))
            return PASS  # its turn ended; the scheduler picks what runs next
        move = supervisor_step(state, ref, runnable_below(state, ref))
        if move.move == "turn":
            return Verdict("continue", move.reason)
        if move.move == "fail":
            return Verdict("fail", move.reason, code=move.reason)
        if move.move == "done":
            waiting = pending_input(view.log, ref)
            if waiting:  # ADR-P27 B4, as above
                return Verdict("continue", person_reason(waiting), code=PERSON_INPUT)
            passed = evaluate_in(state, ref)
            return Verdict("done", facts=(evaluated_fact(ref, passed, on="parent"),))
        return PASS

    def limit(self, view: StopView, c: StopCandidate, proposed: Verdict) -> Verdict | None:
        """The turn limit, then the stop-veto limit (the retry cap), on this instance."""
        if proposed.resumes_turn:
            return None
        ref = self.instance
        task = view.tree.node(ref.task_path).task
        turns = turns_used(view.log, ref.run, ref.instance)
        if turns >= task.turn_limit:
            reason = f"turn limit {task.turn_limit} reached"
            counts = (("turns", turns), ("allowed", task.turn_limit))
            return Verdict("fail", reason, code="turn_limit", counts=counts)
        if c.kind == "finish" and c.instance == ref and proposed.code != PERSON_INPUT:
            vetoes = vetoes_since_entry(view.log, ref.run, ref.instance) + 1
            if vetoes >= task.max_finish_vetoes:
                reason = f"{vetoes} finish requests refused: {proposed.reason}".rstrip(": ")
                counts = (("vetoes", vetoes), ("allowed", task.max_finish_vetoes))
                return Verdict("fail", reason, code="stop_veto_limit", counts=counts)
        return None
