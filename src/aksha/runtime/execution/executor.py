"""``StepExecutor``: the durable step executor on the event store (ADR-P02 D1, D2, D4, D9).

Every step is recorded before it starts and when it ends, under its deterministic step
ID, so a restarted process returns recorded results instead of running steps again:

1. A recorded ``step.completed`` is returned as is, ``replayed=True``; nothing runs.
2. A recorded ``step.started`` with another ``args_hash``, kind or name raises
   ``StepMismatch``: the code being resumed is not the code that recorded the run.
   Otherwise ``step.started`` commits first (U2a).
3. A dangling step (started, never completed) is finished by its semantics:
   ``at_least_once`` runs again; ``at_most_once`` runs again only if its
   ``step.dispatched`` never committed, and is otherwise closed ``interrupted`` with code
   ``outcome_unknown``, never run again (the replay policy for a body with no ledger).
   ``reconcile`` belongs to the outbox: :meth:`StepExecutor.step` refuses it
   (``effect_needs_outbox``), and an effectful tool call goes through
   :meth:`StepExecutor.effect_step`.
4. Each attempt runs ``work`` on a worker thread with a real ``Deadline``. A timed-out
   attempt is abandoned, not killed; if its value arrives later it is recorded as
   ``attempt.discarded`` by its hash and dropped, on the calling thread. A step that
   declares a ``KillableUnit`` is the exception (ADR-P14 D5): at the deadline the kill
   boundary ends the unit's process group, and the step records ``error{timeout}`` with
   ``kill`` (``interrupted{outcome_unknown}`` for an ``at_most_once`` one).
5. A retryable failure (``TransientStepError`` or a timeout, with its code in
   ``retry_on``) records ``step.failed`` and sleeps the backoff on the injected clock.
   Attempts are numbered from the recorded attempt facts (``budgets.limits.next_attempt``),
   so a restart never resets them: ``max_retries=2`` is three attempts across every
   process that runs the step (ADR-P14 D3).
   An ``at_most_once`` step is never retried; its timeout is ``outcome_unknown``. A step
   with a unit is never retried either: an exec timeout repeats (ADR-P14 D4), and a unit
   is one process, so a new attempt needs a new unit, which only the caller can make.
6. Only the live attempt's ``StepOutput`` becomes ``step.completed``, with the events and
   artifact rows it carries, in one transaction (U2).
7. Each attempt's active time is recorded as ``duration_ms`` on its ``step.failed`` or
   ``step.completed``, read on the injected clock; a timed-out attempt records its timeout.
   Budget accounting sums them with the backoffs (ADR-P14 D2).
8. A person's stop (ADR-P18 D5): a new step of a run with a pending stop request never
   starts; it is recorded ``step.completed{outcome: skipped, code: run_stopped}`` alone, so
   the turn ends at once and the next stop candidate ends the run. A dangling step of such
   a run starts no new attempt either: it is closed ``cancelled{run_stopped}`` (``skipped``
   for an ``at_most_once`` step never dispatched, ``outcome_unknown`` for one that was). A
   recorded step still returns its result. A running step finishes, because a thread
   cannot be killed, unless it declares a ``KillableUnit``.
9. The kill boundary (ADR-P18 D6): while a step with a ``unit`` runs, the calling thread
   reads the stop requests every ``stop_poll_ms``; on a stop it asks the unit to settle,
   waits the grace, then terminates it, and records ``step.completed{outcome: cancelled}``
   for an ``at_least_once`` step (no external effects) or ``{outcome: effects_unknown}``
   for an ``at_most_once`` one, with ``kill`` saying how the unit ended and the step's
   ``on_unknown``, so the model or a person is told; recovery lists every
   ``effects_unknown`` step of an open run in its report. The attempt is abandoned; a
   value it returns later is discarded as in item 4.
10. An effectful tool call (:meth:`StepExecutor.effect_step`, ADR-P03 D1) commits its
    ``outbox.enqueued`` in the step intent's transaction, with the authority re-checked,
    and the outbox worker then calls the system of record and commits ``outbox.completed``
    with the step's ``step.completed`` in one transaction. A dangling effectful step is
    finished through its entry: claimed entries are asked by key first, and nothing calls
    the system outside the outbox. A step whose key is already enqueued (a rebuild run's
    equal request) reaches the first entry and its receipt.

An exception that is not an ``Exception`` (a ``KeyboardInterrupt``, a process exit, a
test's simulated crash) is re-raised with nothing recorded, as a real crash would leave it.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from aksha.runtime.artifacts.blobs import inflate, offload_events
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.budgets.limits import ATTEMPT_TYPES, next_attempt
from aksha.runtime.cancellation.kill import kill_boundary
from aksha.runtime.cancellation.ports import KillableUnit
from aksha.runtime.cancellation.requests import STOP_VIEW_TYPES, stop_before_step
from aksha.runtime.cancellation.types import RUN_STOPPED, KillOutcome, KillPolicy
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution import facts
from aksha.runtime.execution.errors import EffectNeedsOutbox, StepMismatch, TransientStepError
from aksha.runtime.execution.ports import Clock
from aksha.runtime.execution.types import (
    OUTCOME_UNKNOWN,
    DanglingStep,
    Deadline,
    StepId,
    StepKind,
    StepOutput,
    StepPolicy,
    StepResult,
)
from aksha.runtime.outbox.errors import OutboxRefused
from aksha.runtime.outbox.keys import Resolve
from aksha.runtime.outbox.outbox import enqueue
from aksha.runtime.outbox.types import EffectSpec
from aksha.runtime.outbox.worker import OutboxWorker, ended_step_result
from aksha.runtime.policy.events import args_hash as hash_args
from aksha.runtime.projections.view import ArtifactsView, LogView, load_view
from aksha.runtime.storage.errors import AlreadyRecorded
from aksha.runtime.storage.ports import EventStore

STEP_EXCEPTION = "step_exception"
"""The code of a step whose work raised an ordinary exception: a bug, never retried."""

TIMEOUT = "timeout"
"""The code of an attempt whose deadline passed."""

SKIPPED = "skipped"
"""The outcome of a step a stop kept from starting (ADR-P18 D5)."""

DEFAULT_KILL = KillPolicy()


@dataclass
class _Attempt:
    """One attempt's mailbox, shared by the calling thread and the worker under a lock."""

    step: StepId
    number: int
    lock: threading.Lock
    done: bool = False
    abandoned: bool = False
    output: StepOutput | None = None
    error: BaseException | None = None


class StepExecutor:
    """The :class:`~aksha.runtime.execution.ports.DurableStepExecutor` for one owner."""

    def __init__(
        self,
        store: EventStore,
        owner: str,
        clock: Clock,
        *,
        kill: KillPolicy = DEFAULT_KILL,
        stop_poll_ms: int = 50,
        outbox: OutboxWorker | None = None,
        blobs: BlobStore | None = None,
    ) -> None:
        """Record ``owner``'s steps in ``store`` (a writer), timing them on ``clock``.

        ``kill`` bounds the kill boundary; ``stop_poll_ms`` is how often a running step
        with a unit reads the stop requests; ``outbox`` is the owner's outbox worker,
        which :meth:`effect_step` needs. With ``blobs``, a step result whose payloads are
        over the 64 KiB cap has its largest values moved to the blob store (decision 25),
        and a recorded value is read back whole; without it such a result is refused
        ``payload_too_large``.
        """
        if stop_poll_ms <= 0:
            raise ValueError("stop_poll_ms is positive")
        self._store = store
        self._owner = owner
        self._clock = clock
        self._kill = kill
        self._stop_poll_ms = stop_poll_ms
        if outbox is not None and outbox.owner != owner:
            raise ValueError(f"the outbox worker runs {outbox.owner}'s entries, not {owner}'s")
        self._outbox = outbox
        self._blobs = blobs
        self._late: queue.SimpleQueue[_Attempt] = queue.SimpleQueue()

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
        """Return ``step_id``'s recorded result, or run ``work`` and record it (module doc).

        ``unit`` is where ``work`` runs when the host can end it (ADR-P18 D6). A
        ``reconcile`` policy is refused (``effect_needs_outbox``): use :meth:`effect_step`.
        """
        if policy.semantics == "reconcile":
            raise EffectNeedsOutbox(
                message=f"{step_id}: a reconcile step is an outbox entry; call effect_step"
            )
        self.drain_late()
        sid = str(step_id)
        started = self._store.by_dedup_key(self._owner, facts.started_key(sid))
        if started is not None:
            _check_same(started, sid, kind, name, args_hash)
        done = self._store.by_dedup_key(self._owner, facts.completed_key(sid))
        if done is not None:
            return self._recorded(step_id, done)
        stopped = self._stop_pending(step_id.run)
        if started is None:
            if stopped:
                return self._skip(step_id, kind, name)
            intent = facts.step_started(step_id, kind, name, args_hash, policy, extra)
            self._append(facts.STEP_INTENT, intent)
        attempt = self._next_attempt(step_id)
        if started is not None:
            closed = self._finish_dangling(step_id, kind, name, policy, attempt, stopped)
            if closed is not None:
                return closed
        return self._run(step_id, kind, name, work, policy, attempt, unit)

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
        """Run tool ``name``'s external effect for ``request`` through the outbox (ADR-P03 D1).

        ``step.started`` and the entry's ``outbox.enqueued`` commit in one transaction
        (U2a), after the worker's authority re-check; a refusal records ``step.started``
        then ``step.completed{error}`` with the refusal's code, and nothing is enqueued.
        The worker then runs the entry and commits ``outbox.completed`` (or
        ``outbox.abandoned``) with this step's ``step.completed`` (U2). The step's
        semantics and ``on_unknown`` are the effect's; its retries are the worker's
        policy for ``name``. ``resolve`` pins each key value to its exact version.
        """
        if self._outbox is None:
            raise EffectNeedsOutbox(message=f"{step_id}: {name} has no outbox worker to run it")
        worker = self._outbox
        target = spec.system or name
        if not worker.serves(target):  # before anything is committed, so nothing is half-sent
            raise EffectNeedsOutbox(
                message=f"{step_id}: {name} sends to {target!r}, which the outbox worker lacks"
            )
        policy = replace(worker.policy(name), semantics=spec.semantics, on_unknown=spec.on_unknown)
        hashed = hash_args(request)
        self.drain_late()
        sid = str(step_id)
        started = self._store.by_dedup_key(self._owner, facts.started_key(sid))
        if started is not None:
            _check_same(started, sid, "tool", name, hashed)
        done = self._store.by_dedup_key(self._owner, facts.completed_key(sid))
        if done is not None:
            return self._recorded(step_id, done)
        if started is not None:
            key = started.payload.get("outbox_key")
            if not isinstance(key, str):  # refused before its entry: the refusal is re-judged
                return self._refused_effect(step_id, name, request, spec, approval_id, resolve)
        elif self._stop_pending(step_id.run):
            return self._skip(step_id, "tool", name)
        else:
            view = load_view(self._store, self._owner)
            try:
                out = enqueue(view, name, request, spec, approval_id=approval_id, step=step_id,
                              resolve=resolve, authority=worker.authority)  # fmt: skip
            except OutboxRefused as refused:
                intent = facts.step_started(step_id, "tool", name, hashed, policy,
                                            {**(extra or {}), "route": "outbox"})  # fmt: skip
                self._append(facts.STEP_INTENT, intent)
                return self._error(step_id, name, refused.code)
            key = out.entry.key
            fields = {**(extra or {}), "route": "outbox", "outbox_key": key}
            intent = facts.step_started(step_id, "tool", name, hashed, policy, fields)
            boundary_events = (intent,) if out.event is None else (intent, out.event)
            self._store.append(self._owner, facts.STEP_INTENT, boundary_events)
        entry = worker.finish(key)
        done = self._store.by_dedup_key(self._owner, facts.completed_key(sid))
        if done is not None:
            return self._recorded(step_id, done, replayed=False)
        if entry is None or entry.state in ("enqueued", "dispatched"):
            raise EffectNeedsOutbox(message=f"{step_id}: entry {key} is held by another worker")
        # The entry is another step's or a command's (an equal request): end with its outcome.
        with contextlib.suppress(AlreadyRecorded):
            self._append(facts.STEP_RESULT, ended_step_result(entry, step_id))
        stored = self._store.by_dedup_key(self._owner, facts.completed_key(sid))
        assert stored is not None
        return self._recorded(step_id, stored, replayed=False)

    def recorded(self, step_id: StepId) -> StepResult | None:
        """The recorded result of ``step_id``, or None when it has no ``step.completed``."""
        done = self._store.by_dedup_key(self._owner, facts.completed_key(str(step_id)))
        return None if done is None else self._recorded(step_id, done)

    def refuse(self, step_id: StepId, refusal: NewEvent) -> StepResult:
        """Record the policy gate's refusal of a call as the step's one fact (ADR-P11 D9).

        ``refusal`` is ``policy.events.decision_event`` of a ``Refuse``: a
        ``step.completed{outcome: refused}`` with no ``step.started``, since nothing ran. A
        step already recorded returns its result, so a resumed turn writes nothing twice.
        """
        if refusal.type != "step.completed" or refusal.payload.get("outcome") != "refused":
            raise ValueError("refuse records a step.completed{outcome: refused} only")
        if refusal.step != str(step_id):
            raise ValueError(f"the refusal names step {refusal.step}, not {step_id}")
        found = self.recorded(step_id)
        if found is not None:
            return found
        (stored,) = self._append(facts.STEP_RESULT, refusal)
        return self._recorded(step_id, stored, replayed=False)

    def skip(self, step_id: StepId, kind: StepKind, name: str, code: str) -> StepResult:
        """Record a step the run's own state keeps from starting: ``skipped`` with ``code``.

        The agent loop records each call position after a finish, a question, an approval
        request or a spent budget this way (ADR-P06 D5), so its body never runs. A step
        that already started is refused: it is not this step's to skip.
        """
        found = self.recorded(step_id)
        if found is not None:
            return found
        sid = str(step_id)
        if self._store.by_dedup_key(self._owner, facts.started_key(sid)) is not None:
            raise StepMismatch(message=f"{sid} started; a started step is never skipped")
        event = facts.step_completed(step_id, kind, name, SKIPPED, attempts=0, code=code)
        (stored,) = self._append(facts.STEP_RESULT, event)
        return self._recorded(step_id, stored, replayed=False)

    def drain_late(self) -> int:
        """Record ``attempt.discarded`` for every abandoned attempt that has since returned."""
        count = 0
        while True:
            try:
                late = self._late.get_nowait()
            except queue.Empty:
                return count
            value: Any = late.output.value if late.output is not None else None
            if late.error is not None:
                value = {"raised": type(late.error).__name__}
            event = facts.attempt_discarded(late.step, late.number, facts.value_hash(value))
            self._append(facts.ATTEMPT_FAILED, event)
            count += 1

    # --- the steps of :meth:`step` -----------------------------------------------------

    def _finish_dangling(
        self,
        step_id: StepId,
        kind: StepKind,
        name: str,
        policy: StepPolicy,
        attempt: int,
        stopped: bool,
    ) -> StepResult | None:
        """Close a step a crash left open, or None when it is safe to run it again.

        With a stop pending (``stopped``) no new attempt starts: the step is closed.
        """
        sid = str(step_id)
        if policy.semantics == "at_most_once":
            dispatched = self._store.by_dedup_key(self._owner, facts.dispatched_key(sid))
            if dispatched is not None:
                dangling = DanglingStep(
                    sid, step_id.run, step_id.instance, kind, name, "at_most_once",
                    policy.on_unknown, dispatched=True,
                    attempts=int(dispatched.payload["attempt"]),
                )  # fmt: skip
                (stored,) = self._append(facts.STEP_RESULT, facts.unknown_completion(dangling))
                return self._recorded(step_id, stored, replayed=False)
            if not stopped:
                return None  # the body never started: running it is its first run
            outcome = SKIPPED  # the body never started, and now it never will
        elif stopped:
            outcome = "cancelled"
        else:
            return None
        closed = facts.step_completed(step_id, kind, name, outcome, attempts=attempt - 1,
                                      code=RUN_STOPPED)  # fmt: skip
        (stored,) = self._append(facts.STEP_RESULT, closed)
        return self._recorded(step_id, stored, replayed=False)

    def _refused_effect(
        self,
        step_id: StepId,
        name: str,
        request: Mapping[str, Any],
        spec: EffectSpec,
        approval_id: str | None,
        resolve: Resolve | None,
    ) -> StepResult:
        """Finish an effectful step that started with no entry: its refusal, judged again.

        Its intent committed with no key, so the authority refused it; a crash before its
        result commits leaves it dangling. Nothing is enqueued now either: authority that
        holds again needs a new step, never this one's.
        """
        assert self._outbox is not None
        view = load_view(self._store, self._owner)
        try:
            enqueue(view, name, request, spec, approval_id=approval_id, step=step_id,
                    resolve=resolve, authority=self._outbox.authority)  # fmt: skip
        except OutboxRefused as refused:
            return self._error(step_id, name, refused.code)
        return self._error(step_id, name, "authority_changed")

    def _error(self, step_id: StepId, name: str, code: str) -> StepResult:
        event = facts.step_completed(step_id, "tool", name, "error", attempts=0, code=code)
        (stored,) = self._append(facts.STEP_RESULT, event)
        return self._recorded(step_id, stored, replayed=False)

    def _run(
        self,
        step_id: StepId,
        kind: StepKind,
        name: str,
        work: Callable[[Deadline], StepOutput],
        policy: StepPolicy,
        attempt: int,
        unit: KillableUnit | None = None,
    ) -> StepResult:
        once = policy.semantics == "at_most_once"
        while True:
            if once:
                self._append(facts.DISPATCH, facts.step_dispatched(step_id, attempt))
            code, output, spent, kill = self._attempt(
                step_id, attempt, work, policy.timeout_ms, unit
            )
            self.drain_late()
            if kill is not None and code == RUN_STOPPED:
                effects = policy.semantics != "at_least_once"
                event = facts.step_completed(
                    step_id, kind, name, "effects_unknown" if effects else "cancelled",
                    attempts=attempt, code=RUN_STOPPED, duration_ms=spent, kill=kill,
                    on_unknown=policy.on_unknown if effects else None,
                )  # fmt: skip
                (stored,) = self._append(facts.STEP_RESULT, event)
                return self._recorded(step_id, stored, replayed=False)
            if kill is not None:  # an exec timeout: the unit was ended, never retried
                failed = facts.step_failed(
                    step_id, attempt, code, retried=False, backoff_ms=0, duration_ms=spent
                )
                self._append(facts.ATTEMPT_FAILED, failed)
                if once:
                    event = facts.step_completed(
                        step_id, kind, name, "interrupted", attempts=attempt,
                        code=OUTCOME_UNKNOWN, on_unknown=policy.on_unknown, duration_ms=0,
                        kill=kill,
                    )  # fmt: skip
                else:
                    event = facts.step_completed(step_id, kind, name, "error", attempts=attempt,
                                                 code=code, duration_ms=0, kill=kill)  # fmt: skip
                (stored,) = self._append(facts.STEP_RESULT, event)
                return self._recorded(step_id, stored, replayed=False)
            if output is not None:
                return self._complete(step_id, kind, name, output, attempt, duration_ms=spent)
            if once and code == TIMEOUT:
                event = facts.step_completed(
                    step_id, kind, name, "interrupted", attempts=attempt,
                    code=OUTCOME_UNKNOWN, on_unknown=policy.on_unknown, duration_ms=spent,
                )  # fmt: skip
                (stored,) = self._append(facts.STEP_RESULT, event)
                return self._recorded(step_id, stored, replayed=False)
            retryable = not once and unit is None and code in policy.retry_on
            retried = retryable and attempt <= policy.max_retries
            backoff = policy.delay_ms(attempt) if retried else 0
            failed = facts.step_failed(
                step_id, attempt, code, retried=retried, backoff_ms=backoff, duration_ms=spent
            )
            self._append(facts.ATTEMPT_FAILED, failed)
            if not retried:
                event = facts.step_completed(
                    step_id, kind, name, "error", attempts=attempt, code=code, duration_ms=0
                )
                (stored,) = self._append(facts.STEP_RESULT, event)
                return self._recorded(step_id, stored, replayed=False)
            self._clock.sleep(backoff / 1000)
            attempt += 1

    def _attempt(
        self,
        step_id: StepId,
        number: int,
        work: Callable[[Deadline], StepOutput],
        timeout_ms: int,
        unit: KillableUnit | None = None,
    ) -> tuple[str, StepOutput | None, int, KillOutcome | None]:
        """Run one attempt on a worker thread and say how it ended.

        The answer is ``(code, None, ms, None)``, ``("", out, ms, None)``,
        ``(run_stopped, None, ms, kill)`` when a stop ended its unit, or
        ``(timeout, None, ms, kill)`` when its deadline did. ``ms`` is the attempt's active
        time on the injected clock; a timeout is its deadline.
        """
        box = _Attempt(step_id, number, threading.Lock())
        began = self._clock.now()
        finished = threading.Event()
        deadline = Deadline(self._clock.mono_ms() + timeout_ms, self._clock.mono_ms)

        def target() -> None:
            output: StepOutput | None = None
            error: BaseException | None = None
            try:
                output = work(deadline)
            except BaseException as exc:  # handed to the calling thread, never swallowed
                error = exc
            with box.lock:
                box.output, box.error, box.done = output, error, True
                if box.abandoned:
                    self._late.put(box)
            finished.set()

        threading.Thread(target=target, name=f"step {step_id}", daemon=True).start()
        kill: KillOutcome | None = None
        cause = RUN_STOPPED
        if unit is None:
            finished.wait(timeout_ms / 1000)
        else:
            watched = self._watch(step_id.run, finished, deadline, unit)
            if watched is not None:
                kill, cause = watched
        spent = max(0, round((self._clock.now() - began) / timedelta(milliseconds=1)))
        with box.lock:
            if kill is not None:
                box.abandoned = not box.done
                if cause == TIMEOUT:
                    return TIMEOUT, None, timeout_ms, kill
                return RUN_STOPPED, None, spent, kill
            if not box.done:
                box.abandoned = True
                return TIMEOUT, None, timeout_ms, None
        if box.error is not None:
            if not isinstance(box.error, Exception):
                raise box.error  # a crash: nothing recorded, as if the process died here
            if isinstance(box.error, TransientStepError):
                return box.error.code, None, spent, None
            return STEP_EXCEPTION, None, spent, None
        if not isinstance(box.output, StepOutput):
            return STEP_EXCEPTION, None, spent, None
        return "", box.output, spent, None

    def _watch(
        self, run: str, finished: threading.Event, deadline: Deadline, unit: KillableUnit
    ) -> tuple[KillOutcome, str] | None:
        """Wait for the attempt, reading the stop requests; end the unit on a stop or timeout.

        Returns how the kill boundary ended the unit and why (``run_stopped`` or
        ``timeout``), or None when the attempt finished on its own.
        """
        poll = self._stop_poll_ms / 1000
        while not finished.wait(min(poll, deadline.remaining_ms() / 1000)):
            if deadline.expired():
                cause = TIMEOUT  # ADR-P14 D5: an exec's timeout kills its process group
            elif self._stop_pending(run):
                cause = RUN_STOPPED
            else:
                continue
            outcome = kill_boundary(unit, self._kill)
            finished.wait(self._kill.reap_seconds)
            return outcome, cause
        return None

    def _stop_pending(self, run: str) -> bool:
        """Whether ``run`` has a pending stop request, read from the store (ADR-P18 D5)."""
        events = tuple(self._store.read(self._owner, run=run, types=STOP_VIEW_TYPES))
        if not any(e.type == "run.stop_requested" for e in events):
            return False
        seq = events[-1].seq
        view = LogView(self._owner, seq, events, ArtifactsView((), seq))
        return stop_before_step(view, run)

    def _skip(self, step_id: StepId, kind: StepKind, name: str) -> StepResult:
        """Record a step a pending stop keeps from starting (ADR-P18 D5)."""
        event = facts.step_completed(step_id, kind, name, SKIPPED, attempts=0, code=RUN_STOPPED)
        (stored,) = self._append(facts.STEP_RESULT, event)
        return self._recorded(step_id, stored, replayed=False)

    def _complete(
        self,
        step_id: StepId,
        kind: StepKind,
        name: str,
        output: StepOutput,
        attempts: int,
        *,
        reconciled: bool = False,
        duration_ms: int | None = None,
    ) -> StepResult:
        event = facts.step_completed(
            step_id, kind, name, output.outcome, attempts=attempts, value=output.value,
            code=output.code, reconciled=reconciled, duration_ms=duration_ms,
            usage=output.usage,
        )  # fmt: skip
        events = (*output.events, event)
        if self._blobs is not None:
            events = offload_events(self._store.catalogue, self._blobs, events)
        stored = self._store.append(self._owner, facts.STEP_RESULT, events, output.artifacts)
        return self._recorded(step_id, stored[-1], replayed=False)

    def _next_attempt(self, step_id: StepId) -> int:
        """One more than the highest attempt recorded for the step (failed or discarded)."""
        events = self._store.read(self._owner, run=step_id.run, types=ATTEMPT_TYPES)
        return next_attempt(events, str(step_id))

    def _recorded(self, step_id: StepId, event: Event, *, replayed: bool = True) -> StepResult:
        """The result ``event`` records, its value read back whole from any blob stub."""
        if self._blobs is not None:
            event = replace(event, payload=inflate(event.payload, self._blobs))
        return _recorded(step_id, event, replayed=replayed)

    def _append(self, boundary: str, event: Any) -> tuple[Event, ...]:
        return self._store.append(self._owner, boundary, (event,))


def _check_same(started: Event, sid: str, kind: str, name: str, args_hash: str) -> None:
    p = started.payload
    recorded = (p.get("kind"), p.get("name", p.get("tool")), p.get("args_hash"))
    if recorded != (kind, name, args_hash):
        raise StepMismatch(
            message=f"{sid} was recorded as {recorded}, and is now reached as "
            f"{(kind, name, args_hash)}: the run's code or arguments changed"
        )


def _recorded(step_id: StepId, event: Event, *, replayed: bool = True) -> StepResult:
    p = event.payload
    value = p.get("value")
    return StepResult(
        step_id=step_id,
        outcome=p["outcome"],
        value=value if isinstance(value, Mapping) else None,
        attempts=int(p.get("attempts", 0)),
        replayed=replayed,
        code=p.get("code"),
    )
