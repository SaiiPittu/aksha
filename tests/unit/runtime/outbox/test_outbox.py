"""The outbox projection and ``enqueue``: an entry is a fact in the caller's transaction."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.outbox import facts
from aksha.runtime.outbox.errors import OutboxRefused
from aksha.runtime.outbox.keys import idempotency_key
from aksha.runtime.outbox.outbox import enqueue, entries, pending_outbox
from aksha.runtime.outbox.types import EffectSpec, OutboxEntry
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.storage.errors import AlreadyRecorded
from aksha.runtime.storage.ports import EventStore
from tests.support.events import START

SEND = EffectSpec(system="mailer", key_fields=("/document", "/to"), semantics="reconcile")
ARGS = {"document": "packet/P1@v2", "to": "r-7"}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    s = open_store(tmp_path / "aksha.sqlite", FakeClock(START), writer=True)
    yield s
    s.close()


def test_enqueue_builds_the_entry_and_its_event(store: EventStore) -> None:
    out = enqueue(load_view(store, "o"), "send", ARGS, SEND, approval_id="A-0001")
    assert out.event is not None
    assert out.entry.key == idempotency_key("send", "o", ARGS, SEND)
    assert (out.entry.state, out.entry.attempts, out.entry.target) == ("enqueued", 0, "mailer")
    assert out.event.type == "outbox.enqueued"
    (stored,) = store.append("o", facts.ENQUEUE, (out.event,))
    assert stored.dedup_key == f"outbox:{out.entry.key}"
    assert stored.payload["request"] == ARGS
    assert stored.payload["approval_id"] == "A-0001"


def test_enqueue_of_a_key_already_enqueued_returns_the_entry_and_no_event(
    store: EventStore,
) -> None:
    first = enqueue(load_view(store, "o"), "send", ARGS, SEND)
    assert first.event is not None
    store.append("o", facts.ENQUEUE, (first.event,))
    again = enqueue(load_view(store, "o"), "send", dict(ARGS), SEND)
    assert again.event is None
    assert again.entry.key == first.entry.key
    assert again.entry.seq == 1


def test_a_key_reused_with_a_different_request_is_refused_and_writes_nothing(
    store: EventStore,
) -> None:
    # ADR-P03 B2: a change outside the key fields is not the same effect. Returning the first
    # entry would hand the caller the first request's receipt for a request it did not send.
    first = enqueue(load_view(store, "o"), "send", ARGS, SEND)
    assert first.event is not None
    store.append("o", facts.ENQUEUE, (first.event,))
    changed = {**ARGS, "subject": "a different cover note"}
    assert idempotency_key("send", "o", changed, SEND) == first.entry.key  # same key
    with pytest.raises(OutboxRefused) as e:
        enqueue(load_view(store, "o"), "send", changed, SEND)
    assert e.value.code == "key_reused_with_different_request"
    assert first.entry.key in e.value.message
    (entry,) = entries(load_view(store, "o")).values()
    assert entry.request == ARGS  # the first entry stands, unchanged


def test_a_second_enqueued_event_for_one_key_is_refused_by_the_store(store: EventStore) -> None:
    out = enqueue(load_view(store, "o"), "send", ARGS, SEND)
    assert out.event is not None
    store.append("o", facts.ENQUEUE, (out.event,))
    with pytest.raises(AlreadyRecorded):
        store.append("o", facts.ENQUEUE, (out.event,))


def test_authority_is_rechecked_in_the_deciding_transaction(store: EventStore) -> None:
    def authority(entry: OutboxEntry, view: LogView) -> str | None:
        return "hash_mismatch" if entry.request["to"] == "r-8" else None

    view = load_view(store, "o")
    assert enqueue(view, "send", ARGS, SEND, authority=authority).event is not None
    with pytest.raises(OutboxRefused) as e:
        enqueue(view, "send", {**ARGS, "to": "r-8"}, SEND, authority=authority)
    assert e.value.code == "hash_mismatch"


def test_projection_follows_an_entry_through_its_states(store: EventStore) -> None:
    out = enqueue(load_view(store, "o"), "send", ARGS, SEND)
    assert out.event is not None
    key = out.entry.key
    store.append("o", facts.ENQUEUE, (out.event,))
    assert [e.state for e in pending_outbox(load_view(store, "o"))] == ["enqueued"]
    store.append("o", facts.WORKER, (facts.claimed(key, 1),))
    (entry,) = pending_outbox(load_view(store, "o"))
    assert (entry.state, entry.attempts) == ("dispatched", 1)
    store.append(
        "o", facts.WORKER, (facts.failed(key, 1, "transient", retried=True, backoff_ms=5),)
    )
    store.append("o", facts.WORKER, (facts.claimed(key, 2),))
    receipt = {"message_id": "m-1"}
    store.append("o", facts.WORKER, (facts.completed(key, "ledger-1", receipt, attempts=2),))
    assert pending_outbox(load_view(store, "o")) == ()
    done = entries(load_view(store, "o"))[key]
    assert (done.state, done.attempts, done.receipt) == ("completed", 2, receipt)


def test_abandoned_and_unknown_entries_are_not_pending(store: EventStore) -> None:
    views = []
    for to, reason in (("r-1", "retries_exhausted"), ("r-2", "outcome_unknown")):
        out = enqueue(load_view(store, "o"), "send", {**ARGS, "to": to}, SEND)
        assert out.event is not None
        store.append("o", facts.ENQUEUE, (out.event,))
        store.append("o", facts.WORKER, (facts.abandoned(out.entry.key, reason, attempts=1),))
        views.append(out.entry.key)
    view = load_view(store, "o")
    assert pending_outbox(view) == ()
    states = [entries(view)[k].state for k in views]
    assert states == ["abandoned", "outcome_unknown"]
    assert entries(view)[views[0]].reason == "retries_exhausted"
