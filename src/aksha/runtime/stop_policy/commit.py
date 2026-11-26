"""Committing a stop decision (ADR-P16 D3, step 6): the one module here that holds a store.

The decision's facts commit in one transaction (U3), the ``stop.decided`` record last. Its
dedup key ``stop:{run}/w{nnn}`` (and ``run.ended``'s ``rend:{run}``) make a restart that
reaches the same decision write nothing: the store refuses the whole transaction with
``AlreadyRecorded``, which here means "a restart got here first".
"""

from __future__ import annotations

import contextlib

from aksha.runtime.stop_policy.types import StopDecision
from aksha.runtime.storage.errors import AlreadyRecorded
from aksha.runtime.storage.ports import EventStore

STOP_DECISION = "U3"
"""The boundary every stop decision commits in."""


def commit_stop_decision(store: EventStore, owner: str, d: StopDecision) -> bool:
    """Commit ``d`` in one transaction (U3); False when a restart already recorded it."""
    with contextlib.suppress(AlreadyRecorded):
        store.append(owner, STOP_DECISION, d.facts)
        return True
    return False
