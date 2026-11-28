"""What an agent declares to run its items under a supervisor: ``PortfolioSpec``.

The kit is generic: it never names a domain. An agent says where its item keys are listed,
which Task is the cheap triage tier every item gets and which Task is the deep tier only
admitted items get, how one item's typed columns are computed from its typed artifacts
(``card``), which column orders the board (``score``), and what the deep tier's result
says about an item (``worth``: would the deep work have kept it). Every number the code
enforces is here too: the tier cap, the deep-tier budget, the batch cap, the audit sample
and its seed, and the item count at or below which no supervisor turn is taken at all.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

CardFn = Callable[[Any, str], Mapping[str, Any]]
"""``(task_view, key) -> columns``: pure, from the item's typed artifacts only."""

WorthFn = Callable[[Any, str], "bool | None"]
"""``(task_view, key) -> keep?``: the deep tier's verdict on an item; None before it ran."""

REASON_CHARS = 120
"""The longest reason a directive carries (super-agent design section 3.1)."""
TITLE_CHARS = 40
"""The longest title a board row shows."""
NOTES_CHARS = 1_500
"""The longest free-text hypotheses field of the supervisor's notes."""
PLAN_CHARS = 2_000
"""The longest plan (goal, tier rule and budget split together)."""
MAX_QUESTIONS = 20
MAX_ENTRIES = 30
MAX_COLUMNS = 12
"""The most typed columns a card shows on the board."""


@dataclass(frozen=True)
class Tier:
    """The deep tier: its worker Task and the bounds code enforces on admission.

    ``effort`` is the model turns admission counts for one item at this tier: the most it
    may cost when its Tasks declare turn limits (at least their sum), else its planned
    cost, with the run budget as the backstop (decision 42); ``cap`` is the most items
    admitted.
    """

    id: str
    task: str
    effort: int
    cap: int


@dataclass(frozen=True)
class PortfolioSpec:
    """One agent's portfolio: items, tiers, card, and every bound the code enforces.

    ``supervisor`` is the supervisor Task's id (a top-level Task); ``triage`` and
    ``deep.task`` are its children's ids. ``items`` is ``(kind, path)``: the field of an
    unkeyed artifact listing the item keys. ``basis`` is the triage tier's output kind,
    whose version a directive records as the basis of its decision.

    ``budget_turns`` is the deep tier's share of the run budget: a commit is refused when
    (admitted plus audited) x ``deep.effort`` exceeds it. ``batch_cap`` bounds how many
    items one commit may newly admit. ``audit_sample`` dropped items, chosen by a seeded
    hash, get deep work anyway; ``max_miss_rate`` is the share of audited items the deep
    tier may find worth keeping before the goal says "re-open drops".
    """

    agent: str
    supervisor: str
    items: tuple[str, str]
    triage: str
    basis: str
    deep: Tier
    card: CardFn
    score: str
    worth: WorthFn
    title: str | None = None
    budget_turns: int = 60
    batch_cap: int = 50
    audit_sample: int = 3
    audit_seed: int = 0
    max_miss_rate: float = 0.0
    supervise_above: int = 8
    page_rows: int = 50

    def full(self, kind: str) -> str:
        """A kind in the agent's namespace: ``directive`` is ``<agent>.directive``."""
        return kind if "." in kind else f"{self.agent}.{kind}"
