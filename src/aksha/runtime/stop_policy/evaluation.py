"""Stop-policy evaluation (ADR-P16 D3): one evaluation per stop candidate, one record.

:func:`evaluate_stop` is pure and writes nothing. In order:

1. Read the budget accounting (the meter), before any policy judges.
2. Derive the stack, outer to inner (``stack.py``).
3. Ask the hold predicate (D6) for the run and the candidate's instance. A pending stop
   request is the Run policy's STOP ``stopped``; an open interrupt holding the run or the
   instance is that interrupt policy's HOLD. Human state wins by position: no policy can
   propose work first.
4. Judge inner to outer. PASS offers the candidate to the next policy out. DONE or FAIL
   pops the instance's policies and offers the candidate to the Run policy as ``next``.
   PUSH offers it to the child (the policies inside the pusher, which passed, leave the
   stack): ``enter`` for a new instance, ``next`` for one already running. CONTINUE,
   HOLD and STOP propose the decision. An instance this evaluation popped FAIL is not
   picked again in it (``StopView.failed``, ADR-P15 S6): one the rebuild plan still
   lists stays blocked, and the Run policy goes on to other work or ends the run.
5. Apply the limits to a proposed CONTINUE, outer to inner: the Run's budget first (a spent
   dimension becomes the budget interrupt's HOLD), then the Task's turn limit and stop-veto
   limit (a FAIL that pops and goes back to step 4). The first limit that bites decides.
6. Build one transaction: the facts the verdicts imply (withdrawals, entries, completion
   check evaluations), the budget interrupt, the run's ending on STOP, and the
   ``stop.decided`` record last.

Only CONTINUE spends budget, one model turn each (its ``turn``); a resumed turn charges
nothing. The record holds counts, never times, so two fake-model runs give equal records.
``commit.py`` commits the transaction; the record's dedup key ``stop:{run}/w{nnn}`` makes
a restart that reaches the same decision write nothing. Nothing here holds a store.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from types import MappingProxyType
from typing import Any, Literal

from aksha.runtime.budgets.facts import budget_reached
from aksha.runtime.budgets.limits import turns_used
from aksha.runtime.budgets.meter import meter
from aksha.runtime.cancellation.ending import ending_facts
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution.types import StepId
from aksha.runtime.finalization.reserve import held_back
from aksha.runtime.finalization.unknowns import unknowns_at_end
from aksha.runtime.inputs.reducers import pending_input
from aksha.runtime.inputs.types import INPUT_CONSUMED
from aksha.runtime.interrupts.reducers import INTERRUPT_RAISED, INTERRUPT_VOIDED, pending_interrupts
from aksha.runtime.projections.view import LogView
from aksha.runtime.stop_policy import hold as holds
from aksha.runtime.stop_policy.core import PERSON_INPUT, InterruptPolicy, TaskPolicy, run_ref
from aksha.runtime.stop_policy.errors import StopRefused
from aksha.runtime.stop_policy.ports import StopPolicy
from aksha.runtime.stop_policy.stack import derive_stack, task_group
from aksha.runtime.stop_policy.types import (
    PolicyVerdict,
    RunScope,
    StopCandidate,
    StopDecision,
    StopView,
    Verdict,
)
from aksha.runtime.tasks.status import STOP_DECIDED, all_instances, run_state
from aksha.runtime.tasks.types import InstanceRef

RUN_ENDED_REASON = "run_ended"
"""The reason an interrupt still open when a run ends is voided."""

Popped = Literal["done", "fail"]


def _seq_no(log: LogView, run: str) -> int:
    return 1 + sum(1 for e in log.events if e.run == run and e.type == STOP_DECIDED)


def with_facts(view: StopView, facts: Iterable[NewEvent]) -> StopView:
    """``view`` with ``facts`` appended as if committed: what the rest of one evaluation reads.

    The facts get sequence numbers after the log's last; they are never stored as such.
    """
    log = view.log
    events = list(log.events)
    at = events[-1].at if events else "1970-01-01T00:00:00+00:00"
    seq = log.seq_read
    for f in facts:
        seq += 1
        event = Event(
            f.type, f.payload, run=f.run, instance=f.instance, step=f.step,
            session=f.session, seq=seq, owner=log.owner, at=at, boot=0, schema_version=1,
        )  # fmt: skip
        events.append(event)
    if seq == log.seq_read:
        return view
    return replace(view, log=LogView(log.owner, seq, tuple(events), log.artifacts, log.complete))


def _judge(policy: StopPolicy, view: StopView, c: StopCandidate) -> Verdict:
    """``policy.judge``; a policy that raises is FAIL ``check_raised`` (the Run's: STOP)."""
    try:
        return policy.judge(view, c)
    except Exception as exc:  # a bug is a recorded, replayable outcome, never a crash
        reason = f"check_raised: {type(exc).__name__}: {exc}"
        if isinstance(policy, TaskPolicy) or c.instance is not None:
            return Verdict("fail", reason, code="check_raised")
        return Verdict("stop", reason, outcome="failed", code="check_raised")


def _limit(
    stack: tuple[StopPolicy, ...], view: StopView, c: StopCandidate, proposed: Verdict
) -> tuple[StopPolicy, Verdict] | None:
    for policy in stack:
        replaced = policy.limit(view, c, proposed)
        if replaced is not None:
            return policy, replaced
    return None


def _bound(view: StopView, run: str) -> int:
    """More offers than the stack can need: four per instance, plus slack."""
    state = run_state(view.log, view.tree, run)
    return 16 + 4 * sum(1 for _ in all_instances(state))


def evaluate_stop(view: StopView, c: StopCandidate) -> StopDecision:
    """Decide candidate ``c`` (module doc, steps 1 to 6). Pure: writes nothing."""
    log0 = view.log
    seq_no = _seq_no(log0, c.run)
    spent = held_back(log0, view.tree, c.run, meter(log0, c.run, budget=view.budget))
    view = replace(view, meter=spent)  # step 1 (the reserve held back, ADR-P24 D5)
    stack = derive_stack(view, c)  # step 2
    derived = tuple(p.ref for p in stack)
    trail: list[PolicyVerdict] = []
    held = holds.hold_reason(log0, RunScope(c.run))  # step 3
    if held is None and c.instance is not None:
        held = holds.hold_reason(log0, c.instance)
    if held is not None:
        if held.reason == "stop_requested":
            verdict = Verdict("stop", "stop_requested", outcome="stopped", code="stop_requested")
            trail.append((run_ref(c.run), "stop"))
            return _decide(
                view, c, seq_no, derived, trail, stack[0], verdict, None, seq_read=log0.seq_read
            )
        run_held = held.scope == c.run
        policy = next(
            (
                p
                for p in reversed(stack)
                if isinstance(p, InterruptPolicy) and (p.instance is None) == run_held
            ),
            stack[0],
        )
        verdict = Verdict("hold", f"held: {held.reason}", code=held.reason)
        if isinstance(policy, InterruptPolicy):
            verdict = policy.judge(view, c)
        trail.append((policy.ref, verdict.kind))
        deciding = None if run_held else c.instance
        return _decide(
            view, c, seq_no, derived, trail, policy, verdict, deciding, seq_read=log0.seq_read
        )

    facts: list[NewEvent] = []
    pops: list[tuple[str, Popped]] = []
    replaced: tuple[PolicyVerdict, Verdict] | None = None
    working = view
    offer = c
    level = len(stack) - 1
    for _ in range(_bound(view, c.run)):  # step 4
        policy = stack[level]
        verdict = _judge(policy, working, offer)
        trail.append((policy.ref, verdict.kind))
        if verdict.facts:
            facts.extend(verdict.facts)
            working = with_facts(working, verdict.facts)
        if verdict.kind == "continue":  # step 5
            hit = _limit(stack, working, offer, verdict)
            if hit is not None:
                by, new = hit
                replaced = ((policy.ref, "continue"), new)
                trail.append((by.ref, new.kind))
                policy, verdict = by, new
        if verdict.kind == "pass":
            if level == 0:
                raise StopRefused("evaluation_loop", "the Run policy passed")
            level -= 1
            continue
        if verdict.kind in ("done", "fail"):
            ref = offer.instance
            if ref is None:
                raise StopRefused("evaluation_loop", f"{policy.ref} popped no instance")
            kind: Popped = "done" if verdict.kind == "done" else "fail"
            pops.append((ref.instance, kind))
            mark = NewEvent(STOP_DECIDED, {"pops": [[ref.instance, kind]]}, run=c.run)
            working = with_facts(working, (mark,))
            if kind == "fail":  # never picked again in this evaluation (ADR-P15 S6, #95)
                working = replace(working, failed=working.failed | {ref})
            base = next(i for i, p in enumerate(stack) if isinstance(p, TaskPolicy))
            stack = stack[:base]
            level = len(stack) - 1
            offer = c.offered("next", None)
            continue
        if verdict.kind == "push":
            child = verdict.child
            assert child is not None  # Verdict refuses a PUSH without one
            # The policies inside the pusher already passed: they leave the stack.
            stack = stack[: level + 1] + task_group(working, child)
            level = len(stack) - 1
            status = run_state(working.log, working.tree, c.run).status(child)
            # A settled child the scheduler picked is a re-entry for a rebuild (ADR-P05 D7).
            entering = status in ("new", "done", "blocked")
            offer = c.offered("enter" if entering else "next", child)
            continue
        return _decide(
            working, c, seq_no, derived, trail, policy, verdict, offer.instance,
            facts=facts, pops=pops, replaced=replaced, seq_read=log0.seq_read,
        )  # fmt: skip
    raise StopRefused("evaluation_loop", f"candidate {c.kind} of {c.run} did not settle")


def _decide(
    view: StopView,
    c: StopCandidate,
    seq_no: int,
    stack: tuple[str, ...],
    trail: list[PolicyVerdict],
    by: StopPolicy,
    verdict: Verdict,
    instance: InstanceRef | None,
    *,
    facts: Iterable[NewEvent] = (),
    pops: Iterable[tuple[str, Popped]] = (),
    replaced: tuple[PolicyVerdict, Verdict] | None = None,
    seq_read: int = 0,
) -> StopDecision:
    """Step 6: the transaction one decision commits, the record last."""
    run = c.run
    log = view.log
    out = list(facts)
    deciding = instance if verdict.kind in ("continue", "hold") else None
    if verdict.kind == "hold" and verdict.raises is not None:
        deciding = None  # the budget interrupt holds the run, not one instance
        m = view.meter
        assert m is not None  # step 1 read it
        ident = verdict.raises.interrupt
        out.append(budget_reached(run, ident, m))
        payload = {"interrupt": ident, "kind": "budget", "reason": verdict.raises.limit}
        out.append(NewEvent(INTERRUPT_RAISED, payload, run=run))
    if verdict.kind == "stop":
        out.extend(_ending(view, run, verdict))
    record: dict[str, Any] = {
        "walk": seq_no,
        "verdict": verdict.kind,
        "cause": c.cause,
        "candidate": c.kind,
        "stack": list(stack),
        "trail": [list(t) for t in trail],
        "decided_by": by.ref,
        "seq_read": seq_read,
    }
    if c.instance is not None:
        record["candidate_instance"] = c.instance.instance
    if c.after_turn is not None:
        record["after_turn"] = c.after_turn
    if verdict.reason:
        record["reason"] = verdict.reason
    turn: int | None = None
    if verdict.kind == "continue" and deciding is not None:
        if verdict.resumes_turn:
            record["resumes_turn"] = True
        else:
            turn = turns_used(log, run, deciding.instance) + 1
            record["turn"] = turn
    if verdict.kind == "continue" and verdict.code == PERSON_INPUT and deciding is not None:
        waiting = pending_input(log, deciding)
        if waiting:
            record["inputs"] = [i.input_id for i in waiting]
        if turn is not None:  # ADR-P27 D4: a follow-up is read by the turn it authorises
            at = str(StepId(run, deciding.instance, turn=turn))
            out.extend(
                NewEvent(INPUT_CONSUMED, {"input_id": i.input_id, "by": "stop_policy",
                                          "step_id": at}, run=run, instance=deciding.instance)
                for i in waiting
                if i.lane == "follow_up"
            )  # fmt: skip
    if verdict.kind == "stop" and verdict.outcome is not None:
        record["outcome"] = verdict.outcome
    if verdict.kind == "hold":
        ident = verdict.raises.interrupt if verdict.raises is not None else _held_by(by)
        if ident:
            record["interrupt"] = ident
    limit: str | None = None
    counts: dict[str, int] = {}
    if replaced is not None:
        (was, kind), new = replaced
        record["replaced"] = [was, kind]
        limit = new.code or None
        counts = dict(new.counts)
        if limit:
            record["limit"] = limit
        if counts:
            record["counts"] = counts
    popped = tuple(pops)
    if popped:
        record["pops"] = [list(p) for p in popped]
    envelope = None if deciding is None else deciding.instance
    out.append(NewEvent(STOP_DECIDED, record, run=run, instance=envelope))
    return StopDecision(
        seq_no=seq_no,
        candidate=c,
        instance=deciding,
        stack=stack,
        trail=tuple(trail),
        decided_by=by.ref,
        verdict=verdict,
        seq_read=seq_read,
        replaced=None if replaced is None else replaced[0],
        limit=limit,
        counts=MappingProxyType(counts),
        pops=popped,
        turn=turn,
        facts=tuple(out),
    )


def _held_by(policy: StopPolicy) -> str:
    return str(getattr(policy, "interrupt", "") or "")


def _ending(view: StopView, run: str, verdict: Verdict) -> tuple[NewEvent, ...]:
    """The run's ending on STOP: voided interrupts and ``run.ended``."""
    if verdict.outcome == "stopped":
        return ending_facts(view.log, run)
    voided = tuple(
        NewEvent(INTERRUPT_VOIDED, {"interrupt": i, "reason": RUN_ENDED_REASON}, run=run)
        for i in pending_interrupts(view.log, run)
    )
    payload: dict[str, Any] = {"outcome": verdict.outcome, "reason": verdict.reason or "ended"}
    if verdict.outcome in ("partial", "blocked"):
        state = run_state(view.log, view.tree, run)
        blocked = tuple(r.instance for r in all_instances(state) if state.status(r) == "blocked")
        if blocked:
            payload["blockers"] = list(blocked)
        found = unknowns_at_end(state)
        if found:
            payload["unknowns"] = [u.as_json() for u in found]
    return (*voided, NewEvent("run.ended", payload, run=run))


ANSWERS = frozenset(
    {"interrupt.resolved", "interrupt.voided", "budget.granted", "command.accepted"}
)
"""Facts after which a held run has something new to decide: an answer, a void, a grant."""


def awaiting_person(view: LogView, run: str) -> bool:
    """Whether ``run``'s last decision held the whole run and nothing has answered it since.

    Then a restart, or a driver with nothing new, offers no candidate and writes nothing
    (the kill-during-approval case: one HOLD, however many restarts follow).
    """
    last: Event | None = None
    for e in view.events:
        if e.run == run and e.type == STOP_DECIDED:
            last = e
    if last is None or last.payload.get("verdict") != "hold" or last.instance is not None:
        return False
    return not any(e.seq > last.seq and e.run == run and e.type in ANSWERS for e in view.events)


SUMMARY_HELD = "summary_held"
"""The refusal of a summary step while a person's stop or an interrupt holds the work."""

SUMMARY_BUDGET = "summary_budget"
"""The refusal of a summary step once a budget dimension (less the reserve) is spent."""


def summary_refusal(view: StopView, ref: InstanceRef) -> str | None:
    """Whether the run and Task limits approve a summary step before ``ref``'s turn (ADR-P08 D7).

    A summary is a model call that spends budget, so it is judged like a turn, on the same
    log: refused ``summary_held`` while the hold predicate holds the run or the instance
    (a pending stop, an open interrupt), and ``summary_budget`` once the meter with the
    finalization reserve held back reports a spent dimension. None approves. Pure; the
    turn itself was authorised by a CONTINUE, which already passed the turn limit.
    """
    log = view.log
    if holds.hold_reason(log, RunScope(ref.run)) is not None:
        return SUMMARY_HELD
    if holds.hold_reason(log, ref) is not None:
        return SUMMARY_HELD
    spent = held_back(log, view.tree, ref.run, meter(log, ref.run, budget=view.budget))
    if spent.exhausted():
        return SUMMARY_BUDGET
    return None
