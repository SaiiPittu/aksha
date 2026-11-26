"""Invariants: rules that hold on every episode whatever the case is about.

The cascade runs every registered criterion of the ``invariant`` layer on every episode,
as ``must``, whether or not a case lists it (:func:`aksha.evals.grade.grade`): a double
send fails a case about prices. ``no_unbound_send`` (in :mod:`.forbidden`) is one too.

``inv.one_row_per_key`` reads the fixture receiver's ledger: at most one delivery per
effect key. It needs nothing but the ledger, so it gates from the first milestone. The
invariants that need the runtime's recorded stop decisions and invalidation semantics
(``inv.stop_decisions_recompute``, ``inv.no_effect_on_possibly_stale``) come with the agents.
"""

from __future__ import annotations

from collections import Counter

from aksha.evals.bundle import EpisodeRecord
from aksha.evals.case import EvalCase
from aksha.evals.grade import Check, Criterion, GradeContext, failed, passed


def one_row_per_key(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """At most one ledger row per effect key."""
    counts = Counter(str(row.get("effect_key")) for row in rec.ledger)
    twice = sorted(k for k, n in counts.items() if n > 1)
    if twice:
        return failed(*(f"{k}: {counts[k]} deliveries" for k in twice))
    return passed(f"{len(rec.ledger)} deliveries, {len(counts)} keys")


CRITERIA = (
    Criterion(
        "inv.one_row_per_key", "invariant", one_row_per_key, description="one send per effect key"
    ),
)
