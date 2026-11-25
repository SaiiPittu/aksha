"""The outbox worker: claim, apply, record; retries counted from the store; recovery by key.

A crash is a ``Crash`` (a ``BaseException``) raised inside the system's ``apply``, after
which the store is closed and opened again, as a restarted process would.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.budgets.types import Backoff
from aksha.runtime.execution.types import StepPolicy
from aksha.runtime.outbox import facts
from aksha.runtime.outbox.errors import SystemUnknown
from aksha.runtime.outbox.outbox import enqueue, entries, pending_outbox
from aksha.runtime.outbox.ports import Authority, SystemOfRecord
from aksha.runtime.outbox.types import EffectSpec, OutboxEntry
from aksha.runtime.outbox.worker import OutboxWorker
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.storage.ports import EventStore
from tests.support.events import START
from tests.support.steps import Crash, types
from tests.support.systems import Ledger, NoLedger

SEND = EffectSpec(system="mailer", key_fields=("/document", "/to"), semantics="reconcile")
HOOK = EffectSpec(system="hook", key_fields=("/pipeline",), semantics="at_most_once")
AGAIN = EffectSpec(system="mailer", key_fields=("/document", "/to"), semantics="at_least_once")
ARGS = {"document": "packet/P1@v2", "to": "r-7"}
QUICK = StepPolicy(max_retries=2, backoff=Backoff(base_ms=100, factor=2, cap_ms=1_000))


class Harness:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.clock = FakeClock(START)
        self.store: EventStore = open_store(path, self.clock, writer=True)
        self.ledger = Ledger("mailer")
        self.hook = NoLedger("hook")

    def systems(self) -> dict[str, SystemOfRecord]:
        return {"mailer": self.ledger, "hook": self.hook}

    def worker(self, authority: Authority | None = None) -> OutboxWorker:
        policies = {"send": QUICK, "trigger": QUICK, "resend": QUICK}
        return OutboxWorker(
            self.store, "o", self.clock, self.systems(), policies=policies, authority=authority
        )

    def enqueue(self, tool: str = "send", args: dict[str, str] = ARGS,
                spec: EffectSpec = SEND) -> str:  # fmt: skip
        out = enqueue(load_view(self.store, "o"), tool, args, spec)
        assert out.event is not None
        self.store.append("o", facts.ENQUEUE, (out.event,))
        return out.entry.key

    def restart(self) -> None:
        self.store.close()
        self.store = open_store(self.path, self.clock, writer=True)

    def entry(self, key: str) -> OutboxEntry:
        return entries(load_view(self.store, "o"))[key]


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path / "aksha.sqlite")
    yield harness
    harness.store.close()


def test_drain_claims_applies_and_records_the_receipt(h: Harness) -> None:
    key = h.enqueue()
    (done,) = h.worker().drain()
    assert (done.key, done.state, done.attempts) == (key, "completed", 1)
    assert done.receipt == {"message_id": "m-1"}
    assert types(h.store) == ["outbox.enqueued", "outbox.claimed", "outbox.completed"]
    completed = list(h.store.read("o"))[-1]
    assert completed.payload["ledger_id"] == "mailer-ledger-1"
    assert completed.dedup_key == f"done-outbox:{key}"
    assert list(h.ledger.rows) == [key]


def test_drain_runs_entries_in_enqueue_order_and_skips_finished_ones(h: Harness) -> None:
    first = h.enqueue(args={**ARGS, "to": "r-1"})
    second = h.enqueue(args={**ARGS, "to": "r-2"})
    assert [e.key for e in h.worker().drain()] == [first, second]
    assert h.worker().drain() == ()
    assert h.ledger.applied == [first, second]


def test_a_second_worker_cannot_claim_an_entry_already_claimed(h: Harness) -> None:
    key = h.enqueue()
    h.store.append("o", facts.WORKER, (facts.claimed(key, 1),))
    assert h.worker().drain() == ()  # dispatched by someone else: not this worker's to run
    assert h.ledger.applied == []


def test_a_claim_lost_to_a_racing_worker_skips_the_entry(h: Harness) -> None:
    key = h.enqueue()

    def racing(entry: OutboxEntry, view: LogView) -> str | None:
        # Another worker claims the entry after this one read it as enqueued.
        h.store.append("o", facts.WORKER, (facts.claimed(entry.key, 1),))
        return None

    assert h.worker(authority=racing).drain() == ()
    assert h.ledger.applied == []
    assert h.entry(key).state == "dispatched"


def test_transient_failures_are_retried_with_backoff_on_the_clock(h: Harness) -> None:
    key = h.enqueue()
    h.ledger.transient = 2
    before = h.clock.now()
    (done,) = h.worker().drain()
    assert (done.state, done.attempts) == ("completed", 3)
    assert (h.clock.now() - before).total_seconds() == pytest.approx(0.3)
    failed = [e.payload for e in h.store.read("o") if e.type == "outbox.failed"]
    assert [(p["attempt"], p["retried"], p["backoff_ms"]) for p in failed] == [
        (1, True, 100), (2, True, 200),
    ]  # fmt: skip
    assert list(h.ledger.rows) == [key]


def test_retries_exhausted_abandons_the_entry(h: Harness) -> None:
    key = h.enqueue()
    h.ledger.transient = 10
    (done,) = h.worker().drain()
    assert (done.state, done.reason, done.attempts) == ("abandoned", "retries_exhausted", 3)
    assert h.ledger.rows == {}
    assert h.entry(key).state == "abandoned"


def test_a_code_outside_retry_on_is_not_retried(h: Harness) -> None:
    h.enqueue()
    h.ledger.transient, h.ledger.transient_code = 1, "rate_limited"
    (done,) = h.worker().drain()
    assert (done.state, done.reason, done.attempts) == ("abandoned", "retries_exhausted", 1)


def test_retries_are_counted_from_the_store_across_a_restart(h: Harness) -> None:
    key = h.enqueue()
    h.store.append("o", facts.WORKER, (facts.claimed(key, 1),))
    h.store.append(
        "o", facts.WORKER, (facts.failed(key, 1, "transient", retried=True, backoff_ms=100),)
    )
    h.store.append("o", facts.WORKER, (facts.claimed(key, 2),))
    h.store.append(
        "o", facts.WORKER, (facts.failed(key, 2, "transient", retried=True, backoff_ms=200),)
    )
    h.restart()
    h.ledger.transient = 10
    (done,) = h.worker().recover()
    assert (done.state, done.reason, done.attempts) == ("abandoned", "retries_exhausted", 3)
    assert len(h.ledger.applied) == 1  # one attempt was left, not a fresh three


def test_a_spent_budget_found_after_a_crash_is_abandoned_without_a_call(h: Harness) -> None:
    key = h.enqueue()
    for attempt in (1, 2, 3):  # the last failure was recorded; the process died before the end
        h.store.append("o", facts.WORKER, (facts.claimed(key, attempt),))
        failed = facts.failed(key, attempt, "transient", retried=attempt < 3, backoff_ms=0)
        h.store.append("o", facts.WORKER, (failed,))
    h.restart()
    (done,) = h.worker().recover()
    assert (done.state, done.reason, done.attempts) == ("abandoned", "retries_exhausted", 3)
    assert h.ledger.applied == []


def test_authority_refusal_abandons_before_any_call(h: Harness) -> None:
    key = h.enqueue()

    def authority(entry: OutboxEntry, view: LogView) -> str | None:
        return "target_invalidated"

    (done,) = h.worker(authority=authority).drain()
    assert (done.state, done.reason, done.attempts) == ("abandoned", "target_invalidated", 0)
    assert h.ledger.applied == []
    assert h.entry(key).state == "abandoned"


def test_an_unknown_system_is_refused_before_the_claim(h: Harness) -> None:
    h.enqueue(spec=EffectSpec(system="fax", key_fields=("/to",), semantics="reconcile"))
    with pytest.raises(SystemUnknown) as e:
        h.worker().drain()
    assert e.value.code == "system_unknown"
    assert types(h.store) == ["outbox.enqueued"]


# --- crashes: what a restart finds and what it does -------------------------------------


def test_crash_after_apply_before_outcome_is_reconciled_by_lookup(h: Harness) -> None:
    key = h.enqueue()
    h.ledger.crash_after_apply = True
    with pytest.raises(Crash):
        h.worker().drain()
    assert h.entry(key).state == "dispatched"
    h.restart()
    h.ledger.crash_after_apply = False
    (done,) = h.worker().recover()
    assert (done.state, done.receipt) == ("completed", {"message_id": "m-1"})
    assert h.ledger.lookups == [key]
    assert h.ledger.applied == [key]  # never applied again
    assert list(h.store.read("o"))[-1].payload["reconciled"] is True


def test_crash_before_apply_lands_nothing_and_recovery_applies_once(h: Harness) -> None:
    key = h.enqueue()
    h.ledger.crash_before_apply = True
    with pytest.raises(Crash):
        h.worker().drain()
    h.restart()
    h.ledger.crash_before_apply = False
    (done,) = h.worker().recover()
    assert (done.state, done.attempts) == ("completed", 2)
    assert h.ledger.lookups == [key]
    assert list(h.ledger.rows) == [key]


def test_recovery_refuses_an_unlanded_entry_whose_authority_changed(h: Harness) -> None:
    key = h.enqueue()
    h.ledger.crash_before_apply = True
    with pytest.raises(Crash):
        h.worker().drain()
    h.restart()
    h.ledger.crash_before_apply = False

    def authority(entry: OutboxEntry, view: LogView) -> str | None:
        return "hash_mismatch"

    (done,) = h.worker(authority=authority).recover()
    assert (done.state, done.reason) == ("abandoned", "hash_mismatch")
    assert h.ledger.rows == {}
    assert h.entry(key).state == "abandoned"


def test_landed_phase_records_receipts_and_leaves_the_rest(h: Harness) -> None:
    landed = h.enqueue(args={**ARGS, "to": "r-1"})
    unlanded = h.enqueue(args={**ARGS, "to": "r-2"})
    h.ledger.crash_after_apply = True
    with pytest.raises(Crash):
        h.worker().drain()
    h.store.append("o", facts.WORKER, (facts.claimed(unlanded, 1),))  # died before its call
    h.restart()
    h.ledger.crash_after_apply = False
    (done,) = h.worker().reconcile("landed")
    assert (done.key, done.state) == (landed, "completed")
    assert [e.key for e in pending_outbox(load_view(h.store, "o"))] == [unlanded]
    (finished,) = h.worker().reconcile("unlanded")
    assert (finished.key, finished.state) == (unlanded, "completed")


def test_dispatched_at_most_once_closed_unknown(h: Harness) -> None:
    key = h.enqueue("trigger", {"pipeline": "build"}, HOOK)
    h.hook.crash_after_apply = True
    with pytest.raises(Crash):
        h.worker().drain()
    h.restart()
    h.hook.crash_after_apply = False
    (done,) = h.worker().recover()
    assert (done.state, done.reason) == ("outcome_unknown", "outcome_unknown")
    assert h.hook.fired == [key]  # fired once, never again
    last = list(h.store.read("o"))[-1]
    assert (last.type, last.payload["on_unknown"]) == ("outbox.abandoned", "tell_model")


def test_at_most_once_failure_is_unknown_and_never_retried(h: Harness) -> None:
    h.enqueue("trigger", {"pipeline": "build"}, HOOK)
    h.hook.fail = True
    (done,) = h.worker().drain()
    assert (done.state, done.attempts) == ("outcome_unknown", 1)
    assert len(h.hook.fired) == 1


def test_on_unknown_interrupt_is_recorded_for_a_person_to_check(h: Harness) -> None:
    spec = EffectSpec(system="hook", key_fields=("/pipeline",), semantics="at_most_once",
                      on_unknown="interrupt")  # fmt: skip
    h.enqueue("trigger", {"pipeline": "build"}, spec)
    h.hook.fail = True
    (done,) = h.worker().drain()
    assert (done.state, done.on_unknown) == ("outcome_unknown", "interrupt")


def test_at_least_once_entry_is_applied_again_after_a_crash(h: Harness) -> None:
    key = h.enqueue("resend", ARGS, AGAIN)
    h.ledger.crash_after_apply = True
    with pytest.raises(Crash):
        h.worker().drain()
    h.restart()
    h.ledger.crash_after_apply = False
    (done,) = h.worker().recover()
    assert (done.state, done.attempts) == ("completed", 2)
    assert h.ledger.lookups == []  # not asked: applying again is safe by the key
    assert h.ledger.applied == [key, key]
    assert list(h.ledger.rows) == [key]


def test_recovery_with_nothing_dangling_writes_nothing(h: Harness) -> None:
    h.enqueue()
    h.worker().drain()
    before = types(h.store)
    assert h.worker().recover() == ()
    assert types(h.store) == before
