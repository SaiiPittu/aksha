"""Protocols for durable execution (ADR-P02): ``Clock``, ``IdSource``, ``DurableStepExecutor``.

``Clock`` is the injected clock (``now``, ``sleep``, ``mono_ms``); its system and fake
implementations are adapters in :mod:`aksha.adapters.clock`. ``IdSource`` hands out run
and other IDs counted from the event store (``execution/ids.py``). ``DurableStepExecutor``
is what a run's code calls for every step (``execution/executor.py``); an engine could sit
under it later. ``RunDriver`` is the code that drives one run's steps, which recovery
calls again to resume a run: the agent loop's ``LoopDriver`` (ADR-P06).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Protocol

from aksha.runtime.cancellation.ports import KillableUnit
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.types import (
    Deadline,
    StepId,
    StepKind,
    StepOutput,
    StepPolicy,
    StepResult,
)
from aksha.runtime.outbox.keys import Resolve
from aksha.runtime.outbox.types import EffectSpec
from aksha.runtime.projections.runs import RunSummary


class Clock(Protocol):
    """The injected clock: every recorded time, duration and backoff reads it."""

    def now(self) -> datetime:
        """The current time, timezone-aware, in UTC. Written into each event's ``at``."""
        ...

    def sleep(self, seconds: float) -> None:
        """Wait ``seconds``. Virtual under a fake clock: the time moves, nothing waits."""
        ...

    def mono_ms(self) -> int:
        """Real monotonic milliseconds in every mode; used for deadlines only (ADR-P02)."""
        ...


class IdSource(Protocol):
    """IDs counted from the event store per owner and prefix: ``R-0001``, ``A-0003`` (D3)."""

    def next(self, owner: str, prefix: str) -> str:
        """One more than the highest ``<prefix>-<n>`` the owner's events mention."""
        ...


class DurableStepExecutor(Protocol):
    """Runs each step at most once to completion, recording before and after (D1)."""

    def step(
        self,
        step_id: StepId,
        kind: StepKind,
        name: str,
        args_hash: str,
        work: Callable[[Deadline], StepOutput],
        policy: StepPolicy,
        *,
        extra: Mapping[str, Any] | None = None,
        unit: KillableUnit | None = None,
    ) -> StepResult:
        """Return the recorded result of ``step_id``, or run ``work`` and record it.

        1. A recorded ``step.completed`` is returned with ``replayed=True``.
        2. A recorded ``step.started`` with another ``args_hash``, kind or name raises
           ``StepMismatch``.
        3. ``at_most_once``: ``step.dispatched`` commits alone before the body; a step
           found dispatched and unfinished is closed ``interrupted`` and never run again.
           ``reconcile`` is refused: an effect goes through :meth:`effect_step`.
        4. Attempts are counted from ``step.failed`` and ``attempt.discarded``.
        5. Only the live attempt's value, on the calling thread, becomes ``step.completed``.
        """
        ...

    def recorded(self, step_id: StepId) -> StepResult | None:
        """The recorded result of ``step_id``, or None when it has no ``step.completed``."""
        ...

    def refuse(self, step_id: StepId, refusal: NewEvent) -> StepResult:
        """Record the policy gate's refusal of a call (``step.completed{refused}``), once."""
        ...

    def skip(self, step_id: StepId, kind: StepKind, name: str, code: str) -> StepResult:
        """Record a step that never starts, ``skipped`` with ``code``, once (ADR-P06 D5)."""
        ...

    def effect_step(
        self,
        step_id: StepId,
        name: str,
        request: Mapping[str, Any],
        spec: EffectSpec,
        *,
        extra: Mapping[str, Any] | None = None,
        approval_id: str | None = None,
        resolve: Resolve | None = None,
    ) -> StepResult:
        """Run an effectful tool call through the outbox (ADR-P03 D1).

        ``outbox.enqueued`` commits with ``step.started``; ``outbox.completed`` (or
        ``outbox.abandoned``) commits with ``step.completed``.
        """
        ...


class RunDriver(Protocol):
    """Drives one run's steps through an executor; recovery calls it again to resume a run.

    Driving a run again reaches its recorded steps first, which return their recorded
    results, so it continues at the first step with no recorded outcome.
    """

    def __call__(self, run: RunSummary, steps: DurableStepExecutor, /) -> None:
        """Drive ``run`` until it ends or holds. Any two-argument callable fits."""
        ...
