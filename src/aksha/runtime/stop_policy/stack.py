"""The stop-policy stack (ADR-P16 D2): a view of the event store, derived at every evaluation.

No event pushes or pops a policy. Outer to inner, for one candidate:

1. the Run policy, while ``run.started`` has no ``run.ended``;
2. an interrupt policy for an open run-scoped interrupt (the budget interrupt);
3. for the candidate's instance: its Task policy, then the agent-wide opt-ins (leaf Tasks
   only, since a plain parent takes no turns), then the Task's own opt-ins in declared
   order, then an interrupt policy while that instance's interrupt is open.

An edit, a revocation or a withdrawn key therefore needs no stack code: the next
derivation reads the log as it is. ``compose_prep`` folds the stack's ``prepare`` answers
outer to inner: briefs in order, tools intersected (narrowed, never widened). A policy
whose ``prepare`` raises fails closed: no tool but the reserved ones, and one line naming
the policy.
"""

from __future__ import annotations

from aksha.runtime.policy.types import StopPolicyNarrowing
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS
from aksha.runtime.stop_policy import hold as holds
from aksha.runtime.stop_policy.builtins import build
from aksha.runtime.stop_policy.core import InterruptPolicy, RunPolicy, TaskPolicy
from aksha.runtime.stop_policy.errors import StopRefused
from aksha.runtime.stop_policy.ports import StopPolicy
from aksha.runtime.stop_policy.types import Prep, RunScope, StopCandidate, StopView
from aksha.runtime.tasks.types import InstanceRef

INTERRUPT_OPEN = "interrupt_open"


def task_group(view: StopView, ref: InstanceRef) -> tuple[StopPolicy, ...]:
    """The policies of one instance, outer to inner (module doc, item 3)."""
    node = view.tree.node(ref.task_path)
    out: list[StopPolicy] = [TaskPolicy(ref)]
    if node.is_leaf:
        out.extend(build(opt, ref) for opt in view.agent_policies)
    out.extend(build(opt, ref) for opt in node.task.stop_policies)
    held = holds.hold_reason(view.log, ref)
    if held is not None and held.reason == INTERRUPT_OPEN:
        found = holds.open_interrupt(view.log, ref.run, ref.instance)
        if found is not None:
            out.append(InterruptPolicy(found[0], ref.run, ref.instance))
    return tuple(out)


def run_open(view: StopView, run: str) -> bool:
    """Whether ``run`` has started and not ended in the view."""
    summary = rebuild(RUNS, view.log).get(run)
    return summary is not None and summary.outcome is None


def derive_stack(view: StopView, c: StopCandidate) -> tuple[StopPolicy, ...]:
    """The stack for ``c``, outer to inner (module doc). ``run_not_open`` after the run ended."""
    if not run_open(view, c.run):
        raise StopRefused("run_not_open", f"run {c.run} is not open")
    out: list[StopPolicy] = [RunPolicy(c.run)]
    held = holds.hold_reason(view.log, RunScope(c.run))
    if held is not None and held.reason == INTERRUPT_OPEN:
        found = holds.open_interrupt(view.log, c.run, None)
        if found is not None:
            out.append(InterruptPolicy(found[0], c.run))
    if c.instance is not None:
        out.extend(task_group(view, c.instance))
    return tuple(out)


def compose_prep(stack: tuple[StopPolicy, ...], view: StopView) -> Prep:
    """Fold the stack's preparations outer to inner (module doc)."""
    brief: list[str] = []
    tools: frozenset[str] | None = None
    narrowed_by: str | None = None
    choice: str | None = None
    for policy in stack:
        try:
            prep = policy.prepare(view)
        except Exception as exc:  # a policy bug narrows to the reserved tools, never widens
            prep = Prep(
                brief=(f"{policy.ref} failed: {type(exc).__name__}",),
                tools=frozenset(),
                narrowed_by=policy.ref,
            )
        brief.extend(prep.brief)
        if prep.tools is not None:
            tools = prep.tools if tools is None else tools & prep.tools
            narrowed_by = prep.narrowed_by or policy.ref
        if prep.tool_choice is not None:
            choice = prep.tool_choice
    if choice is not None and tools is not None and choice not in tools:
        choice = None
    return Prep(tuple(brief), tools, narrowed_by, choice)


def prep_for(view: StopView, ref: InstanceRef) -> Prep:
    """What the stack adds to ``ref``'s next model request (the loop calls it before a turn)."""
    return compose_prep(derive_stack(view, StopCandidate(ref.run, "tools", ref)), view)


def narrowing_of(prep: Prep) -> StopPolicyNarrowing:
    """The part of a preparation the policy gate reads (ADR-P11 layer 4)."""
    return StopPolicyNarrowing(prep.tools, prep.narrowed_by)
