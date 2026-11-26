"""The ``EventStore`` contract (ADR-P01): every storage adapter passes this file unchanged."""

from __future__ import annotations

import signal
import subprocess
import sys
import time

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.runtime.events.catalogue import Catalogue
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.events.errors import (
    BoundaryNotAllowed,
    DedupKeyError,
    PayloadInvalid,
    PayloadTooLarge,
    UnknownEventType,
)
from aksha.runtime.storage.append import SYSTEM_OWNER
from aksha.runtime.storage.errors import (
    AlreadyRecorded,
    ArtifactWithoutEvent,
    LockHeld,
    NotWriter,
    SequenceConflict,
    StoreError,
)
from aksha.runtime.storage.ports import EventStore
from tests.support.backends import Backend
from tests.support.events import (
    artifact_row,
    artifact_row_with,
    probe,
    run_ended,
    run_started,
    saved,
)

# --- ADR-P01 Tests section ---------------------------------------------------------------


def test_update_and_delete_refused(store: EventStore, backend: Backend) -> None:
    store.append("o1", "U2", [saved(), probe(1)], artifacts=[artifact_row()])
    for sql in backend.destructive_sql:
        assert "append-only" in backend.refusal(sql), sql
    assert store.last_seq("o1") == 2
    assert len(store.read_artifacts("o1")) == 1


def test_lock_released_after_kill(backend: Backend, clock: FakeClock, catalogue: Catalogue) -> None:
    first = backend.open(clock, writer=True, catalogue=catalogue)  # creates the schema
    first.close()
    with subprocess.Popen(
        [sys.executable, "-c", backend.writer_child_source()], stdout=subprocess.PIPE, text=True
    ) as child:
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "locked"
            with pytest.raises(LockHeld) as e:
                backend.open(clock, writer=True, catalogue=catalogue)
            assert e.value.pid == child.pid
            assert e.value.exit_code == 8
            assert e.value.code == "lock_held"
        finally:
            child.send_signal(signal.SIGKILL)
            child.wait()
    # The kernel (SQLite) or the server noticing the closed socket (PostgreSQL) frees the
    # lock; the server may take a moment, so retry briefly.
    deadline = time.monotonic() + 10
    while True:
        try:
            backend.open(clock, writer=True, catalogue=catalogue).close()
            break
        except LockHeld:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


def test_owner_filter_on_every_read(store: EventStore) -> None:
    store.append("o1", "U2", [probe(1), saved()], artifacts=[artifact_row()])
    store.append("o2", "U2", [probe(1), probe(2)])
    store.append("o2", "U1", [run_started()])

    assert {e.owner for e in store.read("o1")} == {"o1"}
    assert {e.owner for e in store.read("o2", types=frozenset({"probe.noted"}))} == {"o2"}
    assert {e.owner for e in store.read("o2", run="R-0001")} == {"o2"}
    assert [e.owner for e in store.read("o1", after_seq=0, upto_seq=10)] == ["o1", "o1"]
    assert store.last_seq("o1") == 2
    assert store.last_seq("o2") == 5
    assert store.last_seq("nobody") == 0
    found = store.by_dedup_key("o2", "probe:R-0001/1")
    assert found is not None and found.owner == "o2"
    assert store.by_dedup_key("o3", "probe:R-0001/1") is None
    assert [a.owner for a in store.read_artifacts("o1")] == ["o1"]
    assert store.read_artifacts("o2") == ()
    sub = store.subscribe("o2")
    try:
        assert {e.owner for e in sub.wait(0)} == {"o2"}
    finally:
        sub.close()
    for read in (
        lambda: list(store.read("")),
        lambda: store.last_seq(""),
        lambda: store.by_dedup_key("", "k"),
        lambda: store.read_artifacts(""),
        lambda: store.subscribe(""),
    ):
        with pytest.raises(StoreError) as e:
            read()
        assert e.value.code == "owner_required"


def test_dedup_key_conflict_rolls_back_whole_boundary(store: EventStore) -> None:
    (first,) = store.append("o1", "U2", [probe(3, text="first")])
    before = store.last_seq("o1")
    batch = [probe(1), probe(2), probe(3, text="again"), probe(4)]
    with pytest.raises(AlreadyRecorded) as e:
        store.append("o1", "U2", batch)
    assert e.value.existing == first
    assert e.value.code == "already_recorded"
    assert store.last_seq("o1") == before
    assert [ev.payload["n"] for ev in store.read("o1")] == [3]


def test_payload_schema_enforced(store: EventStore) -> None:
    with pytest.raises(PayloadInvalid) as e:
        store.append("o1", "U2", [NewEvent("probe.noted", {"n": 1, "text": 5}, run="R")])
    assert e.value.path == "$.text"
    assert "$.text" in str(e.value)
    assert store.last_seq("o1") == 0


def test_payload_over_cap_refused(store: EventStore) -> None:
    with pytest.raises(PayloadTooLarge) as e:
        store.append("o1", "U2", [NewEvent("probe.free", {"blob": "x" * (65 * 1024)})])
    assert e.value.code == "payload_too_large"
    assert store.last_seq("o1") == 0


# --- The rest of the append contract -----------------------------------------------------


def test_append_fills_the_envelope(store: EventStore, clock: FakeClock) -> None:
    (a,) = store.append("o1", "U2", [probe(1)])
    clock.advance(1)
    (b,) = store.append("o1", "U2", [probe(2)])
    assert (a.seq, b.seq) == (1, 2)
    assert a.at == "2026-11-27T09:00:00.000000Z"
    assert b.at == "2026-11-27T09:00:01.000000Z"
    assert a.boot == b.boot == 1
    assert a.schema_version == 1
    assert a.dedup_key == "probe:R-0001/1"
    assert list(store.read("o1")) == [a, b]


def test_payloads_round_trip_exactly(store: EventStore) -> None:
    payload = {"items": [1, -2, "é", None, True, {"k": [1]}], "blob": ""}
    (written,) = store.append("o1", "U2", [NewEvent("probe.free", payload)])
    (read,) = store.read("o1")
    assert read == written
    assert read.payload["items"][5]["k"] == (1,)


def test_append_refuses_unknown_types_and_wrong_boundaries(store: EventStore) -> None:
    with pytest.raises(UnknownEventType):
        store.append("o1", "U2", [NewEvent("nope.never", {})])
    with pytest.raises(BoundaryNotAllowed):
        store.append("o1", "U3", [probe(1)])
    with pytest.raises(StoreError) as e:
        store.append("o1", "U2", [])
    assert e.value.code == "empty_boundary"
    assert store.last_seq("o1") == 0


def test_append_checks_caller_dedup_keys(store: EventStore) -> None:
    ok = NewEvent("probe.noted", {"n": 1, "text": "t"}, run="R", dedup_key="probe:R/1")
    store.append("o1", "U2", [ok])
    wrong = NewEvent("probe.noted", {"n": 2, "text": "t"}, run="R", dedup_key="probe:R/9")
    with pytest.raises(DedupKeyError) as e:
        store.append("o1", "U2", [wrong])
    assert e.value.code == "dedup_key_mismatch"
    with pytest.raises(DedupKeyError) as e:
        store.append("o1", "U2", [probe(5), probe(5)])
    assert e.value.code == "dedup_key_repeated_in_boundary"
    assert store.last_seq("o1") == 1


def test_dedup_keys_are_scoped_to_the_owner(store: EventStore) -> None:
    store.append("o1", "U2", [probe(1)])
    store.append("o2", "U2", [probe(1)])
    assert store.last_seq("o2") == 2


def test_expected_seq_guards_the_append(store: EventStore) -> None:
    (a,) = store.append("o1", "U2", [probe(1)], expected_seq=0)
    store.append("o2", "U2", [probe(1)])  # another owner's commit does not move o1's sequence
    store.append("o1", "U2", [probe(2)], expected_seq=a.seq)
    with pytest.raises(SequenceConflict) as e:
        store.append("o1", "U2", [probe(3)], expected_seq=a.seq)
    assert (e.value.expected, e.value.actual) == (a.seq, 3)
    assert e.value.code == "sequence_conflict"
    assert [ev.payload["n"] for ev in store.read("o1")] == [1, 2]


def test_artifact_rows_commit_with_their_saved_event(store: EventStore) -> None:
    events = store.append("o1", "U2", [probe(1), saved()], artifacts=[artifact_row()])
    (stored,) = store.read_artifacts("o1")
    assert stored.event_seq == events[1].seq
    assert stored.row == artifact_row()
    assert store.read_artifacts("o1", upto_seq=events[0].seq) == ()
    with pytest.raises(ArtifactWithoutEvent):
        store.append("o1", "U2", [probe(2)], artifacts=[artifact_row("note/B@v1")])
    assert store.last_seq("o1") == events[1].seq


def test_read_from_a_cursor(store: EventStore) -> None:
    for n in range(1, 6):
        store.append("o1", "U2", [probe(n)])
    assert [e.seq for e in store.read("o1", after_seq=2)] == [3, 4, 5]
    assert [e.seq for e in store.read("o1", after_seq=2, upto_seq=4)] == [3, 4]
    assert list(store.read("o1", after_seq=5)) == []


def test_readers_cannot_write(store: EventStore, backend: Backend, clock: FakeClock) -> None:
    store.append("o1", "U2", [probe(1)])
    reader = backend.open(clock, writer=False, catalogue=store.catalogue)
    try:
        assert [e.seq for e in reader.read("o1")] == [1]
        with pytest.raises(NotWriter) as e:
            reader.append("o1", "U2", [probe(2)])
        assert e.value.code == "not_writer"
    finally:
        reader.close()


def test_boot_counts_process_starts(
    backend: Backend, clock: FakeClock, catalogue: Catalogue
) -> None:
    s1 = backend.open(clock, writer=True, catalogue=catalogue)
    s1.append("o1", "U2", [probe(1)])
    s1.close()
    s2 = backend.open(clock, writer=True, catalogue=catalogue)
    (e,) = s2.append("o1", "U2", [probe(2)])
    s2.close()
    assert e.boot == 2


def test_owners_with_open_runs(store: EventStore) -> None:
    store.append("o1", "U1", [run_started()])
    store.append("o2", "U1", [run_started()])
    store.append("o2", "U3", [run_ended()])
    store.append(SYSTEM_OWNER, "U7", [NewEvent("recovery.completed", {"last_seq_before_boot": 3})])
    assert store.owners_with_open_runs() == ("o1",)


def test_owners_with_pending_timers(store: EventStore) -> None:
    """ADR-P25 D1: an owner is listed while a timer it scheduled is neither fired nor cancelled."""

    def timer(ident: str) -> NewEvent:
        return NewEvent("timer.scheduled", {
            "timer_id": ident, "cause": "approval_expiry", "after_ms": 1,
            "scope": {"kind": "approval", "approval": "A-0001"},
        })  # fmt: skip

    store.append("o1", "U4", [timer("x:1"), timer("x:2")])
    store.append("o2", "U4", [timer("x:1")])
    store.append("o3", "U4", [timer("x:1")])
    store.append(
        "o1", "U4", [NewEvent("timer.fired", {"timer_id": "x:1", "cause": "approval_expiry"})]
    )
    store.append("o2", "U4", [NewEvent("timer.cancelled", {"timer_id": "x:1", "reason": "moot"})])
    assert store.owners_with_pending_timers() == ("o1", "o3")
    store.append("o1", "U4", [NewEvent("timer.cancelled", {"timer_id": "x:2", "reason": "moot"})])
    assert store.owners_with_pending_timers() == ("o3",)


def test_run_started_needs_a_run(store: EventStore) -> None:
    with pytest.raises(DedupKeyError):
        store.append("o1", "U1", [NewEvent("run.started", {"agent": "a", "cause": "request"})])


def test_system_owner_only_for_recovery(store: EventStore) -> None:
    with pytest.raises(StoreError) as e:
        store.append(SYSTEM_OWNER, "U2", [probe(1)])
    assert e.value.code == "system_owner_reserved"


def test_closed_store_refuses_use(backend: Backend, clock: FakeClock, catalogue: Catalogue) -> None:
    s = backend.open(clock, writer=True, catalogue=catalogue)
    s.close()
    s.close()  # closing twice is harmless
    with pytest.raises(StoreError) as e:
        s.last_seq("o1")
    assert e.value.code == "store_closed"


# --- One schema version per store (both backends) ---------------------------------------


@pytest.mark.parametrize("version", [99, None])
def test_a_store_of_another_schema_version_is_refused(
    backend: Backend, clock: FakeClock, catalogue: Catalogue, version: int | None
) -> None:
    backend.open(clock, writer=True, catalogue=catalogue).close()
    backend.set_schema_version(version)
    for writer in (True, False):
        with pytest.raises(StoreError) as e:
            backend.open(clock, writer=writer, catalogue=catalogue)
        assert e.value.code == "schema_mismatch", writer


def test_the_schema_version_row_is_append_only(store: EventStore, backend: Backend) -> None:
    for sql in ("UPDATE meta SET schema_version = 2", "DELETE FROM meta"):
        assert "append-only" in backend.refusal(sql)


def test_reopening_a_store_checks_and_keeps_it(
    backend: Backend, clock: FakeClock, catalogue: Catalogue
) -> None:
    first = backend.open(clock, writer=True, catalogue=catalogue)
    first.append("o1", "U2", [probe(1)])
    first.close()
    again = backend.open(clock, writer=True, catalogue=catalogue)
    try:
        assert [e.type for e in again.read("o1")] == ["probe.noted"]
    finally:
        again.close()


# --- Both backends accept and refuse the same facts --------------------------------------


def test_nul_is_refused_alike_on_every_backend(store: EventStore) -> None:
    with pytest.raises(PayloadInvalid) as e:
        store.append("o1", "U2", [probe(1, text="a\x00b")])
    assert e.value.path == "$.text"
    with pytest.raises(StoreError) as s:
        store.append("o1", "U2", [saved()], artifacts=[artifact_row_with({"body": "x\x00"})])
    assert s.value.code == "nul_in_content"
    with pytest.raises(StoreError) as s:
        store.append("o1", "U2", [probe(1, run="R\x00")])
    assert s.value.code == "nul_in_field"
    assert store.last_seq("o1") == 0
