"""The two budget facts, built in one place. Pure: each returns a ``NewEvent``.

============================  ========  ===================================================
Event                         Boundary  When
============================  ========  ===================================================
``budget.reached``            U3        the stop policy replaces a CONTINUE with the budget
                                        interrupt; commits with ``interrupt.raised``
``budget.granted``            U4        a person's command answers the budget interrupt
============================  ========  ===================================================
"""

from __future__ import annotations

from collections.abc import Mapping

from aksha.runtime.budgets.meter import Meter
from aksha.runtime.budgets.types import DIMENSIONS, Dimension
from aksha.runtime.events.envelope import NewEvent

STOP_DECISION = "U3"
RESOLUTION = "U4"


def budget_reached(run: str, interrupt_id: str, m: Meter, *, partial: bool = True) -> NewEvent:
    """The first spent dimension of ``m``, its use and allowance; ``partial``: work is kept.

    Raises ``ValueError`` when nothing is spent: the fact records a limit that bit.
    """
    spent = m.exhausted()
    if not spent:
        raise ValueError("budget.reached needs a spent dimension")
    d = spent[0]
    payload = {
        "interrupt_id": interrupt_id,
        "limit": d,
        "used": m.used[d],
        "allowed": m.allowed[d],
        "partial": partial,
    }
    return NewEvent("budget.reached", payload, run=run)


def budget_granted(
    run: str, interrupt_id: str, by: str, extra: Mapping[Dimension, int]
) -> NewEvent:
    """A person's grant of ``extra`` per dimension, answering ``interrupt_id``."""
    unknown = sorted(set(extra) - set(DIMENSIONS))
    if unknown or not extra or any(n < 1 for n in extra.values()):
        raise ValueError(f"a grant adds a positive amount to known dimensions, not {dict(extra)}")
    payload = {"interrupt_id": interrupt_id, "by": by, "extra": dict(extra)}
    return NewEvent("budget.granted", payload, run=run)
