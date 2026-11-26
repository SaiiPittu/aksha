"""The reserved budget (ADR-P24 D5): a share of a run's budget only finalizer Tasks spend.

For an agent that declares a finalizer Task, :func:`held_back` lowers each capped
allowance by its reserve until the run is finalizing, so work that is not a finalizer
reaches the budget interrupt with the reserve still unspent. Once ``run.finalizing`` is
recorded the whole allowance is available again, and only finalizers run (D4), so the
reserve is theirs. An agent with no finalizer sets nothing aside.

The share is a placeholder (PF-2, lean A, ``types.py``): ``RESERVE_SHARE_PERCENT`` of each
capped dimension, rounded down, and ``RESERVE_TURNS_PER_FINALIZER`` model turns per
finalizer Task; never more than ``RESERVE_MAX_PERCENT`` of the allowance.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from aksha.runtime.budgets.meter import Meter
from aksha.runtime.budgets.types import Dimension
from aksha.runtime.finalization.state import finalizing
from aksha.runtime.finalization.types import (
    RESERVE_MAX_PERCENT,
    RESERVE_SHARE_PERCENT,
    RESERVE_TURNS_PER_FINALIZER,
)
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.tree import CompiledTree


def finalizer_paths(tree: CompiledTree) -> tuple[str, ...]:
    """The paths of the finalizer Tasks, in declared order (depth first)."""
    return tuple(node.path for node in tree.walk() if node.task.finalizer)


def reserve_for(tree: CompiledTree, allowed: Mapping[Dimension, int]) -> Mapping[Dimension, int]:
    """The reserve per capped dimension (module doc); empty for a tree with no finalizer."""
    count = len(finalizer_paths(tree))
    if count == 0:
        return MappingProxyType({})
    out: dict[Dimension, int] = {}
    for d, cap in allowed.items():
        wanted = (
            RESERVE_TURNS_PER_FINALIZER * count
            if d == "model_turns"
            else cap * RESERVE_SHARE_PERCENT // 100
        )
        share = min(wanted, cap * RESERVE_MAX_PERCENT // 100)
        if share > 0:
            out[d] = share
    return MappingProxyType(out)


def held_back(view: LogView, tree: CompiledTree, run: str, m: Meter) -> Meter:
    """``m`` with the reserve taken off each allowance, unless ``run`` is finalizing (D5)."""
    if finalizing(view, run) is not None:
        return m
    reserve = reserve_for(tree, m.allowed)
    if not reserve:
        return m
    allowed = {d: cap - reserve.get(d, 0) for d, cap in m.allowed.items()}
    return Meter(m.used, MappingProxyType(allowed))
