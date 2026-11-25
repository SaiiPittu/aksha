"""Refusals of durable execution (ADR-P02), each with a stable code.

``StepIdInvalid`` (``step_id_invalid``): a step ID the grammar does not make.
``StepMismatch`` (``step_mismatch``): a restart reached a recorded step ID with other
arguments, another kind or another name, so the run is not the run that was recorded.
``EffectNeedsOutbox`` (``effect_needs_outbox``): a ``reconcile`` step was run as a plain
step, or an effectful step or a recovery with outbox entries to finish has no outbox worker;
an effect runs only through the outbox (ADR-P03 D1). ``NotRecoveryWriter`` (``not_writer``):
recovery was given a reader. ``TransientStepError``: what a step's work raises for a
retryable failure.
"""

from __future__ import annotations

from aksha.runtime.errors import AkshaError


class StepIdInvalid(AkshaError):
    """A step ID outside the grammar of :mod:`aksha.runtime.execution.types`."""

    default_code = "step_id_invalid"


class StepMismatch(AkshaError):
    """The recorded step under this ID was started with other arguments (D2); the run stops."""

    default_code = "step_mismatch"


class EffectNeedsOutbox(AkshaError):
    """An external effect was asked for outside the outbox, or with no worker to run it."""

    default_code = "effect_needs_outbox"


class NotRecoveryWriter(AkshaError):
    """Recovery appends, so it needs the store handle that holds the writer lock."""

    default_code = "not_writer"


class TransientStepError(AkshaError):
    """Raised by a step's work for a failure worth retrying; ``code`` is matched to ``retry_on``."""

    default_code = "transient"
