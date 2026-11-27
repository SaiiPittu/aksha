"""The portfolio kit: a supervisor Task over per-item worker Tasks (ADR-P15 D7 S5, ADR-P16 S4).

An agent with many items (candidates, contracts, competitors) declares a ``PortfolioSpec``
and builds its supervisor with :func:`supervisor_task`. The kit adds no runtime concept: it
is a supervisor Task (a parent with a brief, ADR-P15 D7) whose children are two keyed
worker Tasks, a key filter, a completion check, a recitation and one store tool.

1. Two tiers. The triage Task runs for every item. The deep Task runs only for items the
   deep tier's key filter keeps (``board.not_deep``): every item while the count is at or
   below ``supervise_above`` (no supervisor turn is taken), else the items admitted by a
   directive plus the audit sample.
2. The board (``board.py``): a projection with one row per item, status, tier,
   ``basis_changed`` and cost, recited to the supervisor every turn through ``Recite``.
   The supervisor's request holds the board and its own turns; no worker transcript, no
   document and no whole artifact.
3. ``commit_round`` (``commit.py``): notes, plan and one directive per decided item, in
   one step's transaction, refused in code past the tier cap, the deep budget and the
   batch cap. The model proposes admissions; code bounds them.
4. The audit: code gives deep work to a seeded sample of the drops and reports a miss
   rate; the goal says "re-open drops" while a miss stays dropped above the threshold.
5. The goal (``board.goal``): every item carded, then decided, then the audit clean, each
   failing check tagged with who clears it, so the supervisor rule decides between a turn
   and a hold.
6. The review (``board.review``): the decisions, the drop list and the audit, for one
   batched review beside the ranking.

The kit imports only :mod:`aksha.runtime.api` and names no domain.
"""

from __future__ import annotations

from dataclasses import replace

from aksha.runtime.api import Extension, Recite, Task, artifact_keys

from .board import (
    Audit,
    audit,
    audit_order,
    audit_sample,
    deep_keys,
    directives,
    drop_list,
    goal,
    items,
    not_deep,
    read_board,
    render,
    review,
    review_of,
    rows,
    supervised,
)
from .commit import TOOL, commit_round, commit_spec, commit_tool
from .kinds import DIRECTIVE, ROUND, STATES, kinds
from .spec import PortfolioSpec, Tier

EXTENSION = Extension(name="portfolio", version="0.1.0", requires_api=">=2.15,<3")
"""The kit contributes nothing by itself: an agent declares the kinds in its own namespace
(``kinds()``) and binds ``commit_round`` to its ``PortfolioSpec`` (``commit_tool``)."""


def turn_ceiling(task: Task) -> int:
    """The most model turns one instance of ``task`` and its descendants may take.

    A Task that works (a leaf, or a parent with a brief) may take its ``turn_limit``; a
    parent without a brief takes none of its own. The runtime fails an instance at its
    limit, so this sum bounds one item's cost at the tier (an agent-wide limit only lowers
    it).
    """
    own = task.turn_limit if (not task.children or task.brief) else 0
    return own + sum(turn_ceiling(c) for c in task.children)


def supervisor_task(
    spec: PortfolioSpec, *, triage: Task, deep: Task, brief: str, turn_limit: int = 6
) -> Task:
    """The supervisor Task over ``triage`` and ``deep``, both keyed by the item listing.

    ``triage`` runs for every item; ``deep`` waits for every triage instance (``needs``)
    and runs only for the items the deep filter keeps. The supervisor's own turns are
    offered only ``commit_round``: no read, search, fetch or save tool, so it cannot do
    item work (the gate enforces the Task's tool list). The deep tier's declared effort
    must cover its Tasks' turn limits (:func:`turn_ceiling`), so ``over_budget`` at commit
    is a real bound and not an estimate the workers can overrun.
    """
    kind, path = spec.items
    if triage.id != spec.triage or deep.id != spec.deep.task:
        raise ValueError("the triage and deep Tasks must be the ones the spec names")
    ceiling = turn_ceiling(deep)
    if spec.deep.effort < ceiling:
        raise ValueError(
            f"the {spec.deep.id} tier declares an effort of {spec.deep.effort} turns but its "
            f"Tasks' turn limits allow {ceiling}: the deep budget would bound nothing"
        )
    return Task(
        spec.supervisor,
        brief=brief,
        tools=(TOOL,),
        writes=(DIRECTIVE, ROUND),
        done=goal(spec),
        stop_policies=(Recite(render(spec)),),
        turn_limit=turn_limit,
        children=(
            replace(triage, per=artifact_keys(kind, path)),
            replace(
                deep,
                per=artifact_keys(kind, path, unless=not_deep(spec)),
                needs=(*deep.needs, triage.id),
            ),
        ),
    )


__all__ = [
    "DIRECTIVE",
    "EXTENSION",
    "ROUND",
    "STATES",
    "TOOL",
    "Audit",
    "PortfolioSpec",
    "Tier",
    "audit",
    "audit_order",
    "audit_sample",
    "commit_round",
    "commit_spec",
    "commit_tool",
    "deep_keys",
    "directives",
    "drop_list",
    "goal",
    "items",
    "kinds",
    "not_deep",
    "read_board",
    "render",
    "review",
    "review_of",
    "rows",
    "supervised",
    "supervisor_task",
    "turn_ceiling",
]
