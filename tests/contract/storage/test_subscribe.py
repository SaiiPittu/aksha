"""``EventStore.subscribe``: a cursor that waits for one owner's new events, on every adapter."""

from __future__ import annotations

import threading
import time

from aksha.adapters.clock.fake import FakeClock
from aksha.runtime.storage.ports import EventStore
from tests.support.backends import Backend
from tests.support.events import probe


def _append_later(store: EventStore, owner: str, n: int, delay: float = 0.2) -> threading.Thread:
    def run() -> None:
        time.sleep(delay)
        store.append(owner, "U2", [probe(n)])

    t = threading.Thread(target=run)
    t.start()
    return t


def test_subscription_starts_at_its_cursor(store: EventStore) -> None:
    for n in (1, 2, 3):
        store.append("o1", "U2", [probe(n)])
    sub = store.subscribe("o1", after_seq=1)
    try:
        assert [e.seq for e in sub.wait(0)] == [2, 3]
        assert sub.cursor == 3
        assert sub.wait(0) == ()
    finally:
        sub.close()


def test_subscription_wakes_on_a_new_commit(store: EventStore) -> None:
    sub = store.subscribe("o1")
    try:
        t = _append_later(store, "o1", 1)
        started = time.monotonic()
        events = sub.wait(5)
        t.join()
        assert [e.payload["n"] for e in events] == [1]
        assert time.monotonic() - started < 4
    finally:
        sub.close()


def test_subscription_ignores_other_owners(store: EventStore) -> None:
    sub = store.subscribe("o1")
    try:
        t = _append_later(store, "o2", 1, delay=0.05)
        assert sub.wait(0.5) == ()
        t.join()
        assert sub.cursor == 0
    finally:
        sub.close()


def test_a_reader_sees_the_writers_commits(
    store: EventStore, backend: Backend, clock: FakeClock
) -> None:
    reader = backend.open(clock, writer=False, catalogue=store.catalogue)
    sub = reader.subscribe("o1")
    try:
        t = _append_later(store, "o1", 7)
        events = sub.wait(5)
        t.join()
        assert [e.payload["n"] for e in events] == [7]
    finally:
        sub.close()
        reader.close()
