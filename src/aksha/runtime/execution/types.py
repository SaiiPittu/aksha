"""Step values: ``StepId``, ``StepSemantics``, ``StepPolicy`` and ``StepResult`` (ADR-P02).

A step ID such as ``R-0002/compare[C1]/t03/c02`` is computed from the run's
structure, never from a clock or a random number.

Built so far: ``StepSemantics`` (D4), which an effectful tool declares in its
``EffectSpec``. ``StepId``, ``StepPolicy`` and ``StepResult`` arrive with the durable
step executor.
"""

from __future__ import annotations

from typing import Literal, get_args

StepSemantics = Literal["at_least_once", "reconcile", "at_most_once"]
"""How a step left dangling by a crash is finished (D4).

``at_least_once``: safe to repeat. ``reconcile``: look the idempotency key up in the
system of record. ``at_most_once``: never repeated; the outcome is recorded as unknown.
"""

STEP_SEMANTICS: frozenset[str] = frozenset(get_args(StepSemantics))
"""The three names, for checks of values that arrive untyped from an agent module."""
