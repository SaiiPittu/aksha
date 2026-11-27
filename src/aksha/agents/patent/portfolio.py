"""The patent agent's portfolio: its candidates under a supervisor Task (ADR-P15 D7 S5).

``SPEC`` binds the portfolio kit (``aksha.extensions.portfolio``) to the patent agent:

1. Items are the candidates the ``portfolio`` artifact lists.
2. Triage is ``extract``, every candidate's; the deep tier is ``deep`` (prior-art search,
   comparison, recommendation), only for the candidates the supervisor admits plus the
   audit sample. With at most ``SUPERVISE_ABOVE`` candidates nothing changes: every
   candidate gets deep work and the supervisor takes no turn.
3. The card (:func:`card`) is computed by code from the extraction: how far it is built,
   its business-priority rank, whether the venture disclosed it in public, how many
   referenced documents are missing, and a triage ``score`` that adds them up. The score
   is a cheap signal on purpose: it counts any public disclosure against a candidate,
   while the rule table (``rules.next_action``) can still put a built, top-priority
   candidate with an unseen element first. The audit exists for that gap.
4. The deep tier's verdict (:func:`worth`) is the rule table's: worth keeping when the
   recommendation is ``prioritize_counsel_review``.

The score orders work; it is never a patentability or novelty view, and no artifact the
agent saves carries it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.extensions.portfolio import PortfolioSpec, Tier

from . import rules

SUPERVISE_ABOVE = 8
"""At or below this many candidates every one gets deep work and no supervisor turn runs."""

PRIOR_ART_TURNS = 6
"""The prior-art Task's turn limit: search, fetch what matches, save, finish."""

SAVE_TURNS = 2
"""The compare and recommend Tasks' turn limit: save, then finish."""

DEEP_EFFORT = PRIOR_ART_TURNS + 2 * SAVE_TURNS
"""Model turns one candidate's deep tier may cost: the sum of its Tasks' turn limits, so
the runtime fails an instance before it overruns what admission counted for it."""

DEEP_CAP = 10
"""The most candidates admitted to the deep tier at once."""

DEEP_BUDGET = DEEP_CAP * DEEP_EFFORT
"""The deep tier's share of the run's model turns: admitted plus audited times the effort."""

AUDIT_SAMPLE = 3
"""Dropped candidates that get deep work anyway, chosen by a seeded hash."""


def _plan_cutoff(view: Any) -> int:
    found = view.latest("portfolio")
    plan = found.content.get("plan") if found is not None else None
    cutoff = plan.get("priority_cutoff") if isinstance(plan, Mapping) else None
    return int(cutoff) if isinstance(cutoff, int) else rules.TOP_PRIORITIES


def columns(extraction: Mapping[str, Any], cutoff: int) -> dict[str, Any]:
    """The card's columns from one extraction's content (pure; module doc, item 3)."""
    status = str(extraction.get("implementation", {}).get("status", "unknown"))
    rank = extraction.get("business_priority", {}).get("rank")
    ranked = isinstance(rank, int) and not isinstance(rank, bool)
    public = any(d.get("public") == "yes" for d in extraction.get("disclosures", ()))
    missing = len(extraction.get("missing_documents", ()) or ())
    built = 2 if status in rules.BUILT else 1 if status == "prototype" else 0
    priority = 2 if ranked and rank <= cutoff else 1 if ranked else 0
    score = built + priority - (2 if public else 0) - (1 if missing else 0)
    return {
        "title": extraction.get("title"),
        "status": status,
        "priority_rank": rank if ranked else None,
        "public_disclosure": public,
        "missing_documents": missing,
        "score": score,
    }


def card(view: Any, key: str) -> Mapping[str, Any]:
    """The board's columns for candidate ``key``, from its current extraction."""
    found = view.latest("extraction", key)
    return {} if found is None else columns(found.content, _plan_cutoff(view))


def worth(view: Any, key: str) -> bool | None:
    """Whether the deep tier would keep ``key``: its recommendation puts it first."""
    found = view.latest("recommendation", key)
    if found is None:
        return None
    return bool(found.content.get("next_action") == rules.NEXT_ACTIONS[0])


SPEC = PortfolioSpec(
    agent="patent",
    supervisor="candidate",
    items=("portfolio", "candidates"),
    triage="extract",
    basis="extraction",
    deep=Tier("deep", "deep", effort=DEEP_EFFORT, cap=DEEP_CAP),
    card=card,
    score="score",
    worth=worth,
    title="title",
    budget_turns=DEEP_BUDGET,
    batch_cap=DEEP_CAP,
    audit_sample=AUDIT_SAMPLE,
    audit_seed=0,
    max_miss_rate=0.0,
    supervise_above=SUPERVISE_ABOVE,
)
