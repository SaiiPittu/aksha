"""A sequential driver over real stop-policy evaluation (ADR-P16), for the stop tests.

``drive_stop`` is the loop's part a test needs: offer a candidate, commit the decision,
act on it. A CONTINUE runs one scripted turn of the deciding instance, whose answer is the
next candidate's kind (``finish``, ``tools``, ``interrupted``, ``malformed``); a HOLD of
one instance offers ``next`` so the run's other work goes on (S1); a HOLD of the run calls
``answer`` (a person) or returns; a STOP returns.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aksha.runtime.budgets.types import Budget, Limits
from aksha.runtime.stop_policy.evaluation import evaluate_stop
from aksha.runtime.stop_policy.types import StopCandidate, StopDecision, StopPolicyOptIn, StopView
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import InstanceRef
from tests.support.budgets import RUN

StopTurn = Callable[[InstanceRef, Any], str]
"""One scripted model turn: returns the candidate kind its end is."""


def view_of(
    ledger: Any,
    tree: CompiledTree,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    agent_policies: tuple[StopPolicyOptIn, ...] = (),
) -> StopView:
    """A ``StopView`` of the ledger's log as it stands now."""
    return StopView(
        ledger.view(), tree, RUN, limits or Limits(), budget, agent_policies=agent_policies
    )


def decide(ledger: Any, tree: CompiledTree, c: StopCandidate, **kw: Any) -> StopDecision:
    """Evaluate ``c`` on the ledger's log and commit the decision (U3)."""
    d = evaluate_stop(view_of(ledger, tree, **kw), c)
    ledger.commit(d.facts)
    return d


def drive_stop(
    ledger: Any,
    tree: CompiledTree,
    turn: StopTurn,
    *,
    answer: Callable[[Any], bool] | None = None,
    first: StopCandidate | None = None,
    max_steps: int = 300,
    **kw: Any,
) -> list[StopDecision]:
    """Drive the run to a STOP (or an unanswered run HOLD); return every decision."""
    decisions: list[StopDecision] = []
    c = first or StopCandidate(RUN, "next")
    for _ in range(max_steps):
        d = decide(ledger, tree, c, **kw)
        decisions.append(d)
        v = d.verdict
        if v.kind == "stop":
            return decisions
        if v.kind == "hold":
            if d.instance is not None:
                c = StopCandidate(RUN, "next")
                continue
            if answer is not None and answer(ledger):
                c = StopCandidate(RUN, "next")
                continue
            return decisions
        assert d.instance is not None
        kind = turn(d.instance, ledger)
        c = StopCandidate(RUN, kind, d.instance, after_turn=d.turn)  # type: ignore[arg-type]
    raise AssertionError("the run did not settle")


def obedient_stop(selection: tuple[str, ...]) -> StopTurn:
    """The demo tree's obedient model: one turn per instance, then a finish."""

    def turn(pick: InstanceRef, ledger: Any) -> str:
        if pick.task_path == "scope":
            ledger.select(*selection)
        elif pick.task_path == "extract":
            ledger.save("item", pick.key, {"of": pick.key})
        elif pick.task_path == "compare":
            ledger.save("cmp", pick.key, {"of": pick.key})
        elif pick.task_path == "report":
            ledger.save("report", None, {"rows": len(selection)})
        return "finish"

    return turn


def records(ledger: Any) -> list[Any]:
    """Every ``stop.decided`` the ledger holds."""
    return [e for e in ledger.view().events if e.type == "stop.decided"]
