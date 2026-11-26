"""Invalidation modes (decision 38, ADR-P05 and ADR-P20 amendment M1): opt-in per agent.

An agent definition declares how much of invalidation its runs get, as ``Agent.invalidation``:

=============  ========================================================================
Mode           What the runtime does for the agent's runs
=============  ========================================================================
``off``        Nothing (the default when nothing is declared). Its versions never read
               as stale, its old tool results are never masked as stale, no recheck
               timer is scheduled for what it read, and no rebuild run is opened for it.
``edit_guard`` ``off``, plus the edit-time check (:mod:`invalidation.guard`): a tool that
               declares ``ToolSpec.edits`` is refused when its target changed since this
               instance last read it, or when it overwrites a whole existing record the
               instance never read. The refusal shows the current version and text.
``notify``     ``edit_guard``, plus stale masking of old tool results and the change
               notice at the next turn boundary (ADR-P05 D4 with ADR-P08, decision 32).
               Its versions still never read as stale, so nothing rebuilds, no diff
               review opens and no approval is revoked because of it.
``rebuild``    The full cascade of ADR-P05: the invalidation graph over its versions,
               rebuild runs, early cutoff, diff reviews and fresh approvals.
=============  ========================================================================

Each mode includes the one before it. The mode is a fact: ``run.started{invalidation}``
records the mode the run's definition declared, so every reader of the log (the view, the
context, the rebuild opener) derives the same answer after a restart. A ``run.started``
without the field was written before decision 38, when every run had the full cascade,
so it reads as ``rebuild``. The agent's instruction file is not an input this setting
governs: it is read and hashed into the definition for every agent, whatever the mode.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, get_args

from aksha.runtime.projections.view import LogView

InvalidationMode = Literal["off", "edit_guard", "notify", "rebuild"]
"""How much of invalidation an agent's runs get (module doc)."""

INVALIDATION_MODES: tuple[str, ...] = get_args(InvalidationMode)
"""The modes in order: each includes the one before it."""

DEFAULT_MODE: InvalidationMode = "off"
"""The mode of a definition that declares none (decision 38)."""

UNRECORDED_MODE: InvalidationMode = "rebuild"
"""The mode of a run whose ``run.started`` records none: one written before decision 38."""

INVALIDATION_FIELD = "invalidation"
"""The ``run.started`` field that records the mode."""


def _at_least(mode: str, floor: InvalidationMode) -> bool:
    return INVALIDATION_MODES.index(mode) >= INVALIDATION_MODES.index(floor)


def guards_edits(mode: str) -> bool:
    """Whether a tool that declares ``edits`` is checked at edit time (``edit_guard`` and up)."""
    return _at_least(mode, "edit_guard")


def masks_stale(mode: str) -> bool:
    """Whether stale tool results are masked and announced (``notify`` and up)."""
    return _at_least(mode, "notify")


def tracks_versions(mode: str) -> bool:
    """Whether the run's versions can read as stale and be rebuilt (``rebuild`` only)."""
    return mode == "rebuild"


def run_modes(view: LogView) -> Mapping[str, InvalidationMode]:
    """Each run in the log to the mode its ``run.started`` recorded (module doc)."""
    out: dict[str, InvalidationMode] = {}
    for e in view.events:
        if e.type == "run.started" and e.run is not None:
            value = e.payload.get(INVALIDATION_FIELD, UNRECORDED_MODE)
            out[e.run] = value if value in INVALIDATION_MODES else UNRECORDED_MODE
    return MappingProxyType(out)


def run_mode(view: LogView, run: str | None) -> InvalidationMode:
    """The mode of ``run``; a run the log does not hold, or none, reads as unrecorded."""
    if run is None:
        return UNRECORDED_MODE
    for e in view.events:
        if e.type == "run.started" and e.run == run:
            value = e.payload.get(INVALIDATION_FIELD, UNRECORDED_MODE)
            return value if value in INVALIDATION_MODES else UNRECORDED_MODE
    return UNRECORDED_MODE
