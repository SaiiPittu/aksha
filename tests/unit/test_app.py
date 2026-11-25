from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import SQLiteEventStore
from aksha.app import StorageConfig, open_event_store
from aksha.runtime.storage.errors import StoreError


def test_storage_config_reads_the_environment() -> None:
    assert StorageConfig.from_env({}) == StorageConfig()
    # SQLite is the default, so a run needs no setup (decision 34); PostgreSQL is opt-in.
    assert StorageConfig().backend == "sqlite"
    assert StorageConfig.from_env({}).backend == "sqlite"
    cfg = StorageConfig.from_env(
        {"AKSHA_STORE": "postgres", "AKSHA_POSTGRES_DSN": "dbname=x", "AKSHA_SQLITE_PATH": "a.db"}
    )
    assert cfg == StorageConfig(
        backend="postgres", sqlite_path=Path("a.db"), postgres_dsn="dbname=x"
    )
    with pytest.raises(StoreError) as e:
        StorageConfig.from_env({"AKSHA_STORE": "mongo"})
    assert e.value.code == "unknown_backend"


def test_open_event_store_opens_sqlite_when_chosen(tmp_path: Path, clock: FakeClock) -> None:
    cfg = StorageConfig.from_env(
        {"AKSHA_STORE": "sqlite", "AKSHA_SQLITE_PATH": str(tmp_path / "aksha.sqlite")}
    )
    store = open_event_store(cfg, clock, writer=True)
    try:
        assert isinstance(store, SQLiteEventStore)
    finally:
        store.close()


def test_open_event_store_opens_sqlite_by_default(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    store = open_event_store(StorageConfig.from_env({}), clock, writer=True)
    try:
        assert isinstance(store, SQLiteEventStore)
        assert (tmp_path / "var" / "aksha.sqlite").exists()
    finally:
        store.close()


def test_open_event_store_needs_a_dsn_for_postgres(clock: FakeClock) -> None:
    with pytest.raises(StoreError) as e:
        open_event_store(StorageConfig.from_env({"AKSHA_STORE": "postgres"}), clock, writer=True)
    assert e.value.code == "dsn_required" and "AKSHA_POSTGRES_DSN" in str(e.value)


def test_open_event_store_opens_postgres(postgres_dsn: str, clock: FakeClock) -> None:
    from aksha.adapters.storage.postgres import PostgresEventStore

    store = open_event_store(
        StorageConfig(backend="postgres", postgres_dsn=postgres_dsn), clock, writer=True
    )
    try:
        assert isinstance(store, PostgresEventStore)
    finally:
        store.close()
