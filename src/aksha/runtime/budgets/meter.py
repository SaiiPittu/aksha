"""Budget accounting: ``Meter`` and the reducer behind it, counted from the event store.

``meter(view, run)`` says how much of each dimension a run (or one Task subtree, its
``scope``) has used and is allowed. There is no counter field anywhere: the numbers are a
reducer over recorded facts (``records.py`` lists which fact counts for what), so a
restarted process rebuilds the same numbers, and the projection cache keeps the rebuild
cheap (ADR-P14 D1, D2).

Allowed is the budget recorded on ``run.started{budget}`` (or the one the caller
passes, such as a Task's narrowed budget, or the runtime default when the run recorded
none), plus the ``extra`` of every ``budget.granted``: a person's recorded command. An
uncapped dimension stays uncapped.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from aksha.runtime.budgets import records
from aksha.runtime.budgets.types import DIMENSIONS, Budget, Dimension
from aksha.runtime.events.envelope import Event
from aksha.runtime.policy.events import is_counted_tool_call
from aksha.runtime.policy.types import MeterReading
from aksha.runtime.projections.cache import ProjectionCache
from aksha.runtime.projections.project import project, rebuild
from aksha.runtime.projections.view import ArtifactsView, LogView

BUDGET_GRANTED = "budget.granted"

DEFAULT_BUDGET = Budget()
"""The runtime default, for a run that recorded no budget of its own."""


def in_scope(instance: str | None, scope: str | None) -> bool:
    """Whether a Task ``instance`` path is ``scope`` or inside its subtree (None: the run)."""
    if scope is None:
        return True
    if instance is None:
        return False
    return instance == scope or instance.startswith((scope + "/", scope + "["))


@dataclass(frozen=True)
class Accounts:
    """The reducer's state for one (run, scope): what was used, recorded and granted."""

    run: str
    scope: str | None = None
    tool_calls: int = 0
    active_ms: int = 0
    tokens: int = 0
    cost_micro_usd: int = 0
    turns: frozenset[tuple[str, ...]] = frozenset()
    recorded: Budget | None = None
    granted: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))

    def used(self) -> Mapping[Dimension, int]:
        """Use per dimension."""
        return MappingProxyType(
            {
                "model_turns": len(self.turns),
                "tool_calls": self.tool_calls,
                "active_ms": self.active_ms,
                "tokens": self.tokens,
                "cost_micro_usd": self.cost_micro_usd,
            }
        )


class BudgetAccounting:
    """Reducer over the facts ``records.py`` names; ``args`` is ``(run, scope)``."""

    name = "budget_accounting"
    reads = frozenset(
        {
            "run.started",
            BUDGET_GRANTED,
            records.STOP_DECIDED,
            "step.started",
            "step.failed",
            "step.completed",
        }
    )

    def initial(self, args: Hashable) -> Accounts:
        """Nothing used and nothing recorded for ``args``, a ``(run, scope)`` pair."""
        if not isinstance(args, tuple) or len(args) != 2:
            raise ValueError(f"budget accounting reads (run, scope), not {args!r}")
        run, scope = args
        return Accounts(run=str(run), scope=None if scope is None else str(scope))

    def apply(self, state: Accounts, event: Event, artifacts: ArtifactsView) -> Accounts:
        """Add what ``event`` counts for, if it belongs to the state's run and scope."""
        e = event
        if e.run != state.run:
            return state
        if e.type == "run.started":
            recorded = e.payload.get("budget")
            if recorded is None:
                return state
            return replace(state, recorded=Budget.from_payload(recorded))
        if e.type == BUDGET_GRANTED:
            granted = dict(state.granted)
            for d, n in (e.payload.get("extra") or {}).items():
                granted[d] = granted.get(d, 0) + int(n)
            return replace(state, granted=MappingProxyType(granted))
        if not in_scope(e.instance, state.scope):
            return state
        turn = records.charged_turn(e)
        if turn is not None:
            return replace(state, turns=state.turns | {turn})
        tokens, cost = records.usage(e)
        return replace(
            state,
            tool_calls=state.tool_calls + (1 if is_counted_tool_call(e) else 0),
            active_ms=state.active_ms + records.active_ms(e),
            tokens=state.tokens + tokens,
            cost_micro_usd=state.cost_micro_usd + cost,
        )


BUDGET_ACCOUNTING = BudgetAccounting()
"""The one instance the runtime uses."""


@dataclass(frozen=True)
class Meter:
    """Budget accounting for one run or scope: use and allowance per dimension.

    ``allowed`` holds only capped dimensions; a dimension missing from it is not capped.
    """

    used: Mapping[Dimension, int]
    allowed: Mapping[Dimension, int]

    def exhausted(self) -> tuple[Dimension, ...]:
        """The capped dimensions whose use has reached the allowance, in report order."""
        return tuple(d for d in DIMENSIONS if d in self.allowed and self.used[d] >= self.allowed[d])

    def remaining(self, d: Dimension) -> int | None:
        """What is left of ``d``, never negative; None when ``d`` is not capped."""
        if d not in self.allowed:
            return None
        return max(0, self.allowed[d] - self.used[d])

    def reading(self) -> MeterReading:
        """The part the policy gate reads (layer 0): the spent dimensions."""
        return MeterReading(frozenset(self.exhausted()))


def meter(
    view: LogView,
    run: str,
    scope: str | None = None,
    *,
    budget: Budget | None = None,
    default: Budget = DEFAULT_BUDGET,
    cache: ProjectionCache | None = None,
) -> Meter:
    """Budget accounting of ``run`` (or of the Task subtree ``scope``) in ``view``.

    Allowed is ``budget`` when given (a Task's narrowed budget), else the budget recorded
    on ``run.started``, else ``default``; every grant is added to a capped dimension.
    With ``cache`` the projection goes through the projection cache (ADR-P01 D3).
    """
    args = (run, scope)
    state = (
        rebuild(BUDGET_ACCOUNTING, view, args)
        if cache is None
        else project(BUDGET_ACCOUNTING, view, args, cache=cache)
    )
    base = budget or state.recorded or default
    allowed: dict[Dimension, int] = {}
    for d in DIMENSIONS:
        cap = base.cap(d)
        if cap is not None:
            allowed[d] = cap + state.granted.get(d, 0)
    return Meter(state.used(), MappingProxyType(allowed))
