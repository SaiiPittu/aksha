"""SQLite-only behaviour; the shared rules are in ``tests/contract/storage``."""

from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import SQLiteEventStore, lock_path, open_store
from aksha.runtime.events.catalogue import Catalogue
from aksha.runtime.storage.chain import ChainBreak
from aksha.runtime.storage.errors import LockHeld, StoreError
from tests.support.backends import SQLiteBackend
from tests.support.events import artifact_row, probe, saved


def test_store_uses_wal_and_full_sync(tmp_path: Path, clock: FakeClock) -> None:
    store = open_store(tmp_path / "aksha.sqlite", clock, writer=True)
    try:
        assert isinstance(store, SQLiteEventStore)
        assert store.pragma("journal_mode") == "wal"
        assert store.pragma("synchronous") == "2"  # FULL
    finally:
        store.close()


def test_lock_file_sits_beside_the_store_and_names_the_holder(
    tmp_path: Path, clock: FakeClock, catalogue: Catalogue
) -> None:
    path = tmp_path / "var" / "aksha.sqlite"
    assert lock_path(path) == tmp_path / "var" / "aksha.lock"
    store = open_store(path, clock, writer=True, catalogue=catalogue)
    try:
        assert lock_path(path).read_text().strip().isdigit()
        with pytest.raises(LockHeld):  # a second writer in the same process is refused too
            open_store(path, clock, writer=True, catalogue=catalogue)
    finally:
        store.close()
    open_store(path, clock, writer=True, catalogue=catalogue).close()


def test_reader_needs_an_existing_store(tmp_path: Path, clock: FakeClock) -> None:
    with pytest.raises(StoreError) as e:
        open_store(tmp_path / "missing.sqlite", clock, writer=False)
    assert e.value.code == "store_missing"
    assert not (tmp_path / "missing.sqlite").exists()


def test_reader_connection_is_read_only(
    tmp_path: Path, clock: FakeClock, catalogue: Catalogue
) -> None:
    path = tmp_path / "aksha.sqlite"
    writer = open_store(path, clock, writer=True, catalogue=catalogue)
    writer.append("o1", "U2", [probe(1)])
    reader = open_store(path, clock, writer=False, catalogue=catalogue)
    try:
        assert reader.pragma("query_only") == "1"
    finally:
        reader.close()
        writer.close()


def _filled(tmp_path: Path, clock: FakeClock, catalogue: Catalogue) -> SQLiteEventStore:
    store = open_store(tmp_path / "var" / "aksha.sqlite", clock, writer=True, catalogue=catalogue)
    store.append("o1", "U2", [probe(1)])
    store.append("o1", "U2", [probe(2)])
    store.append("o1", "U2", [saved()], artifacts=[artifact_row()])
    store.append("o1", "U2", [probe(4)])
    assert store.verify() is None
    return store


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ('{"n": 2, "text": tampered', "payload is not JSON"),
        ("[" * 100_000, "payload is not JSON"),
        ('{"n":2,"text":"x","text":"t"}', "payload is not in canonical JSON"),
        ('{"n": 2, "text": "t"}', "payload is not in canonical JSON"),
    ],
)
def test_a_payload_that_is_not_the_writers_text_is_reported(
    tmp_path: Path, clock: FakeClock, catalogue: Catalogue, payload: str, why: str
) -> None:
    """Text that does not decode, or that decodes to the hashed value but is not canonical
    (a duplicated key SQLite's JSON functions would read differently), is a break."""
    store = _filled(tmp_path, clock, catalogue)
    try:
        SQLiteBackend(tmp_path).tamper(f"UPDATE events SET payload = '{payload}' WHERE seq = 2")
        assert store.verify() == ChainBreak(2, "not_canonical", None, why)
    finally:
        store.close()


def test_an_artifact_content_that_is_not_the_writers_text_is_reported(
    tmp_path: Path, clock: FakeClock, catalogue: Catalogue
) -> None:
    store = _filled(tmp_path, clock, catalogue)
    try:
        backend = SQLiteBackend(tmp_path)
        backend.tamper("""UPDATE artifacts SET content = '{"body": "hello"}'""")
        found = store.verify()
        assert found is not None and (found.seq, found.reason) == (3, "artifact_mismatch")
        assert found.found == "content is not in canonical JSON"
    finally:
        store.close()


def test_a_break_before_an_unreadable_row_is_reported_first(
    tmp_path: Path, clock: FakeClock, catalogue: Catalogue
) -> None:
    store = _filled(tmp_path, clock, catalogue)
    try:
        SQLiteBackend(tmp_path).tamper(
            "UPDATE events SET payload = 'not json' WHERE seq = 4",
            """UPDATE events SET payload = '{"n":1,"text":"edited"}' WHERE seq = 1""",
        )
        found = store.verify()
        assert found is not None and (found.seq, found.reason) == (1, "hash_mismatch")
    finally:
        store.close()
