"""``OutboxWorker``: claims and runs outbox entries after their commit (ADR-P03 D1, D3, D4, D7, D8).

The worker runs one owner's entries, in enqueue order, on the calling thread (in the first
milestone the drainer is the runtime thread, right after the deciding command commits).
For each entry:

1. Re-check authority (an approval for this exact action, the target current). A
   refusal abandons the entry with that code before any call. A tool step's entry whose
   run a person has stopped is abandoned ``run_stopped`` before any call (ADR-P18 D5).
   Both checks run again before every retry, so a stop or a revocation during a backoff
   keeps the next attempt from being sent.
2. Claim the attempt: ``outbox.claimed`` commits alone, under ``oclaim:{key}/a{n}``.
   A claim already recorded means another worker holds the entry, so this one skips it.
3. Call ``apply(key, request)`` on the entry's system of record and record
   ``outbox.completed`` with the receipt. A system that indexes the key answers a
   repeated key with its first receipt, so a retry never acts twice.
4. A ``TransientEffectError`` whose code is in the tool's ``retry_on`` records
   ``outbox.failed`` and sleeps the backoff on the injected clock, then claims the next
   attempt. Attempts are counted from the claims in the store, so a restart never resets
   them; with retries used up the entry is abandoned ``retries_exhausted`` (D8).
5. An ``at_most_once`` entry is never retried: any failure after its claim, and a claim
   found after a crash, is ``outcome_unknown``, with the entry's ``on_unknown`` (D4).

An entry a tool step enqueued (``StepExecutor.effect_step``) ends together with its step:
``outbox.completed`` or ``outbox.abandoned`` and the step's ``step.completed`` commit in
one transaction (U2), on every path (a drain, :meth:`OutboxWorker.finish`, recovery), so
the step's result is never recorded without the entry's end, nor the reverse. The step's
outcome follows the entry's: ``ok`` with the receipt, ``interrupted{outcome_unknown}``,
``cancelled{run_stopped}``, or ``error`` with the abandonment's reason.

Recovery (:meth:`OutboxWorker.reconcile`) finishes the entries a crash left behind, in two
phases (D7), which ``recover_owner`` runs in ADR-P02 D5's order. ``landed``: ask each
claimed ``reconcile`` entry's system by key and record the receipt it holds, never
applying again. ``unlanded``: close each claimed ``at_most_once`` entry as
``outcome_unknown``; run every other entry that did not land, the claimed ones and the
ones enqueued but never claimed, under the same key if authority still holds, or abandon
it with the authority's code. The source-change step of recovery (P05) runs between the
two phases once it is built. Recovery runs before any worker of the process drains,
because a claimed entry is read as a crash leftover.

Not built: the receipt artifact (``EffectSpec.receipt_kind``, S2: ``outbox.completed``
saves no artifact yet), the ledger check (``SystemReceipt.ledger_id`` is recorded but never
compared, so a replaced ledger is not refused), a deadline on ``apply`` (the call runs on
the calling thread until it returns), and one drainer lane per session.

An exception that is not an ``Exception`` (a process exit, a test's simulated crash) is
re-raised with nothing recorded, as a real crash would leave it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import replace
from typing import Literal

from aksha.runtime.cancellation.requests import pending_stop
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution import facts as step_facts
from aksha.runtime.execution.ports import Clock
from aksha.runtime.execution.types import StepId, StepPolicy
from aksha.runtime.outbox import facts
from aksha.runtime.outbox.errors import SystemUnknown, TransientEffectError
from aksha.runtime.outbox.outbox import entries, pending_outbox
from aksha.runtime.outbox.ports import Authority, SystemOfRecord
from aksha.runtime.outbox.types import (
    EFFECT_EXCEPTION,
    OUTCOME_UNKNOWN,
    RETRIES_EXHAUSTED,
    RUN_STOPPED,
    OutboxEntry,
)
from aksha.runtime.projections.view import load_view
from aksha.runtime.storage.errors import AlreadyRecorded
from aksha.runtime.storage.ports import EventStore

Phase = Literal["landed", "unlanded"]
"""The two recovery phases of D7, around the source check."""


class OutboxWorker:
    """Runs one owner's outbox entries against the registered systems of record."""

    def __init__(
        self,
        store: EventStore,
        owner: str,
        clock: Clock,
        systems: Mapping[str, SystemOfRecord],
        *,
        policies: Mapping[str, StepPolicy] | None = None,
        authority: Authority | None = None,
    ) -> None:
        """Run ``owner``'s entries in ``store`` (a writer), calling ``systems`` by name.

        ``policies`` gives each tool's retry policy (a ``StepPolicy`` by tool name;
        ``StepPolicy()`` otherwise). ``authority`` re-checks each entry before a call.
        """
        self._store = store
        self._owner = owner
        self._clock = clock
        self._systems = systems
        self._policies = policies or {}
        self._authority = authority

    @property
    def owner(self) -> str:
        """The owner whose entries this worker runs."""
        return self._owner

    @property
    def authority(self) -> Authority | None:
        """The re-check of a person's authority, which ``enqueue`` also runs."""
        return self._authority

    def policy(self, tool: str) -> StepPolicy:
        """``tool``'s retry policy: the one registered for it, else ``StepPolicy()``."""
        return self._policies.get(tool, StepPolicy())

    def drain(self) -> tuple[OutboxEntry, ...]:
        """Run every entry not yet claimed, in enqueue order; return each as it ended."""
        out: list[OutboxEntry] = []
        for entry in pending_outbox(load_view(self._store, self._owner)):
            if entry.state != "enqueued":
                continue  # claimed: another worker's, or a crash leftover for recovery
            finished = self._run(entry)
            if finished is not None:
                out.append(finished)
        return tuple(out)

    def reconcile(self, phase: Phase) -> tuple[OutboxEntry, ...]:
        """Finish the entries a crash left behind (module docstring); return those ended.

        ``landed`` reads only claimed entries; ``unlanded`` also runs the entries enqueued
        and never claimed, which a crash right after the deciding commit leaves (D7).
        """
        out: list[OutboxEntry] = []
        for entry in pending_outbox(load_view(self._store, self._owner)):
            if phase == "landed":
                finished = self._landed(entry) if entry.state == "dispatched" else None
            else:
                finished = self._unlanded(entry)
            if finished is not None:
                out.append(finished)
        return tuple(out)

    def recover(self) -> tuple[OutboxEntry, ...]:
        """Both phases of recovery, ``landed`` then ``unlanded``, with no source check between."""
        return (*self.reconcile("landed"), *self.reconcile("unlanded"))

    def finish(self, key: str) -> OutboxEntry | None:
        """Bring the entry under ``key`` to an end now, as its state needs; return it.

        An enqueued entry runs; a claimed one is asked first (``landed``) and otherwise
        finished as ``unlanded``; an ended one is returned as it stands. None when the
        owner has no such entry, or another worker holds it.
        """
        entry = entries(load_view(self._store, self._owner)).get(key)
        if entry is None or entry.state not in ("enqueued", "dispatched"):
            return entry
        if entry.state == "dispatched":
            landed = self._landed(entry)
            if landed is not None:
                return landed
            return self._unlanded(entry)
        return self._run(entry)

    # --- the steps -----------------------------------------------------------------------

    def _landed(self, entry: OutboxEntry) -> OutboxEntry | None:
        """Record the receipt of a ``reconcile`` entry that landed, or None."""
        if entry.semantics != "reconcile":
            return None
        receipt = self._system(entry).lookup(entry.key, entry.request)
        if receipt is None:
            return None
        event = facts.completed(
            entry.key, receipt.ledger_id, receipt.body, attempts=entry.attempts, reconciled=True
        )
        return self._end(entry, event)

    def _unlanded(self, entry: OutboxEntry) -> OutboxEntry | None:
        """Close a claimed ``at_most_once`` entry unknown, or run another entry (again)."""
        if entry.semantics == "at_most_once" and entry.state == "dispatched":
            event = facts.abandoned(
                entry.key, OUTCOME_UNKNOWN, attempts=entry.attempts, on_unknown=entry.on_unknown
            )
            return self._end(entry, event)
        return self._run(entry)

    def _run(self, entry: OutboxEntry) -> OutboxEntry | None:
        """Steps 1 to 5 of the module docstring; None when another worker holds the entry."""
        system = self._system(entry)
        policy = self.policy(entry.tool)
        once = entry.semantics == "at_most_once"
        attempt = entry.attempts + 1
        while True:
            # Before every attempt, a retry's too: a stop or a revoked authority that came
            # during the backoff keeps the effect from being sent.
            if self._run_stopped(entry):
                event = facts.abandoned(entry.key, RUN_STOPPED, attempts=attempt - 1)
                return self._end(entry, event)
            refusal = self._refusal(entry)
            if refusal is not None:
                return self._end(entry, facts.abandoned(entry.key, refusal, attempts=attempt - 1))
            if not once and attempt > policy.max_retries + 1:
                event = facts.abandoned(entry.key, RETRIES_EXHAUSTED, attempts=attempt - 1)
                return self._end(entry, event)
            try:
                self._append(facts.claimed(entry.key, attempt))
            except AlreadyRecorded:
                return None
            try:
                receipt = system.apply(entry.key, entry.request)
            except Exception as exc:
                code = exc.code if isinstance(exc, TransientEffectError) else EFFECT_EXCEPTION
                if once:
                    event = facts.abandoned(
                        entry.key, OUTCOME_UNKNOWN, attempts=attempt, on_unknown=entry.on_unknown
                    )
                    return self._end(entry, event)
                transient = isinstance(exc, TransientEffectError) and code in policy.retry_on
                retried = transient and attempt <= policy.max_retries
                backoff = policy.delay_ms(attempt) if retried else 0
                self._append(
                    facts.failed(entry.key, attempt, code, retried=retried, backoff_ms=backoff)
                )
                if not retried:
                    is_transient = isinstance(exc, TransientEffectError)
                    reason = RETRIES_EXHAUSTED if is_transient else EFFECT_EXCEPTION
                    event = facts.abandoned(entry.key, reason, attempts=attempt)
                    return self._end(entry, event)
                self._clock.sleep(backoff / 1000)
                attempt += 1
                continue
            event = facts.completed(entry.key, receipt.ledger_id, receipt.body, attempts=attempt)
            return self._end(entry, event)

    def _run_stopped(self, entry: OutboxEntry) -> bool:
        """Whether ``entry`` is a tool step's and a person's stop of its run is pending."""
        if entry.step is None or entry.run is None:
            return False
        return pending_stop(load_view(self._store, self._owner), entry.run) is not None

    def _refusal(self, entry: OutboxEntry) -> str | None:
        if self._authority is None:
            return None
        return self._authority(entry, load_view(self._store, self._owner))

    def _system(self, entry: OutboxEntry) -> SystemOfRecord:
        system = self._systems.get(entry.target)
        if system is None:
            raise SystemUnknown(message=f"{entry.tool}: no system of record {entry.target!r}")
        return system

    def _end(self, entry: OutboxEntry, event: NewEvent) -> OutboxEntry:
        """Append the entry's last fact (once) and return the entry as the store now has it.

        A step's entry ends in one transaction with the step's result (U2).
        """
        with contextlib.suppress(AlreadyRecorded):
            if entry.step is None:
                self._append(event)
            else:
                result = step_result(entry, event)
                self._store.append(self._owner, step_facts.STEP_RESULT, (event, result))
        return entries(load_view(self._store, self._owner))[entry.key]

    def _append(self, event: NewEvent) -> tuple[Event, ...]:
        return self._store.append(self._owner, facts.WORKER, (event,))


def ended_step_result(entry: OutboxEntry, step: StepId) -> NewEvent:
    """The ``step.completed`` of ``step`` for an entry that has already ended. Pure.

    For a step whose key was already enqueued by another step or a command (an equal
    request): the step ends with the first entry's outcome, and nothing is sent again.
    """
    if entry.state == "completed":
        end = facts.completed(entry.key, entry.ledger_id or "unknown", entry.receipt or {},
                              attempts=entry.attempts)  # fmt: skip
    elif entry.state in ("abandoned", "outcome_unknown"):
        end = facts.abandoned(entry.key, str(entry.reason), attempts=entry.attempts)
    else:
        raise ValueError(f"outbox entry {entry.key} has not ended")
    return step_result(replace(entry, step=str(step)), end)


def step_result(entry: OutboxEntry, end: NewEvent) -> NewEvent:
    """The ``step.completed`` of ``entry``'s step for its last fact ``end``. Pure.

    ``outbox.completed`` is ``ok`` with the key, ledger and receipt as the value; an
    ``outcome_unknown`` abandonment is ``interrupted`` with the entry's ``on_unknown``; a
    stop's is ``cancelled{run_stopped}``; any other abandonment is ``error`` with its reason.
    """
    if entry.step is None:
        raise ValueError(f"outbox entry {entry.key} belongs to no step")
    step = StepId.parse(entry.step)
    p = end.payload
    attempts = int(p.get("attempts", entry.attempts))
    if end.type == "outbox.completed":
        value = {"key": entry.key, "ledger_id": p["ledger_id"], "receipt": p["receipt"]}
        return step_facts.step_completed(
            step, "tool", entry.tool, "ok", attempts=attempts, value=value,
            reconciled=bool(p.get("reconciled", False)),
        )  # fmt: skip
    reason = str(p["reason"])
    if reason == OUTCOME_UNKNOWN:
        return step_facts.step_completed(
            step, "tool", entry.tool, "interrupted", attempts=attempts, code=OUTCOME_UNKNOWN,
            on_unknown=entry.on_unknown,
        )  # fmt: skip
    outcome = "cancelled" if reason == RUN_STOPPED else "error"
    return step_facts.step_completed(step, "tool", entry.tool, outcome, attempts=attempts,
                                     code=reason)  # fmt: skip
