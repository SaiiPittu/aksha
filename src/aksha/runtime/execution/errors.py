"""Refusals of durable execution (ADR-P02), each with a stable code.

``StepIdInvalid`` (``step_id_invalid``): a step ID the grammar does not make.
``StepMismatch`` (``step_mismatch``): a restart reached a recorded step ID with other
arguments, another kind or another name, so the run is not the run that was recorded.
``EffectNeedsOutbox`` (``effect_needs_outbox``): a ``reconcile`` step was run as a plain
step, or an effectful step or a recovery with outbox entries to finish has no outbox worker;
an effect runs only through the outbox (ADR-P03 D1). ``NotRecoveryWriter`` (``not_writer``):
recovery was given a reader. ``TransientStepError``: what a step's work raises for a
retryable failure. ``StepSuspended``: what a step's work raises when only a person acting
outside the run can let it finish (a provider account with no credits left); the step
stays open and the run waits for ``aksha resume``.
"""

from __future__ import annotations

from collections.abc import Mapping

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
    """Raised by a step's work for a failure worth retrying; ``code`` is matched to ``retry_on``.

    ``detail`` is what the failure was, for ``step.failed{error}`` (a provider's error type,
    status and short message); it never carries a key or a request body. ``retry_after_ms``
    is how long the failure asked to wait before the next attempt (a provider's
    ``Retry-After``); the backoff is at least that (``StepPolicy.delay_ms``).
    """

    default_code = "transient"

    def __init__(
        self,
        code: str | None = None,
        message: str = "",
        *,
        detail: Mapping[str, str | int] | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        """As :class:`AkshaError`, plus the failure's ``detail`` and ``retry_after_ms``."""
        super().__init__(code, message)
        self.detail = dict(detail) if detail else None
        self.retry_after_ms = retry_after_ms if retry_after_ms and retry_after_ms > 0 else None


SUSPEND_CODES: frozenset[str] = frozenset({"model_quota_exhausted"})
"""The codes a step may be suspended with: each names a failure a person fixes outside the
run (a provider account with no credits left), after which ``aksha resume`` asks again."""


class StepSuspended(AkshaError):
    """Raised by a step's work when the step cannot finish until a person acts outside the run.

    The executor records the attempt as ``step.failed{code, retried: true}`` with ``detail``
    and raises this again without a ``step.completed``, so the run stays open at this step:
    driving skips it and says ``message``, and a later process (``aksha resume``) runs the
    step's next attempt. ``code`` is one of :data:`SUSPEND_CODES`.
    """

    default_code = "model_quota_exhausted"

    def __init__(
        self,
        code: str | None = None,
        message: str = "",
        *,
        detail: Mapping[str, str | int] | None = None,
    ) -> None:
        """As :class:`AkshaError`, plus the failure's ``detail``; ``code`` is a suspend code."""
        super().__init__(code, message)
        if self.code not in SUSPEND_CODES:
            raise ValueError(f"{self.code!r} is not a code a step is suspended with")
        self.detail = dict(detail) if detail else None
