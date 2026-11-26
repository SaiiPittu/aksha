import os
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


def test_open_blob_store_is_a_folder_of_blobs(tmp_path: Path) -> None:
    from aksha.adapters.blobs.filesystem import FilesystemBlobStore
    from aksha.app import open_blob_store

    config = StorageConfig.from_env({"AKSHA_BLOB_PATH": str(tmp_path / "b")})
    assert config.blob_path == tmp_path / "b"
    assert StorageConfig.from_env({}).blob_path == Path("var/blobs")
    blobs = open_blob_store(config)
    assert isinstance(blobs, FilesystemBlobStore) and blobs.get(blobs.put(b"x")) == b"x"


def test_sources_setting_names_a_folder_per_source() -> None:
    from aksha.app import RuntimeConfig

    cfg = RuntimeConfig.from_env({"AKSHA_SOURCES": f"corpus=a{os.pathsep}notes=b"})
    assert cfg.sources == {"corpus": Path("a"), "notes": Path("b")}
    assert RuntimeConfig.from_env({}).sources == {}
    with pytest.raises(StoreError) as e:
        RuntimeConfig.from_env({"AKSHA_SOURCES": "corpus"})
    assert e.value.code == "source_invalid"


def test_a_source_setting_no_agent_declares_is_refused(tmp_path: Path) -> None:
    from aksha.app import load_definitions, source_reader
    from tests.support.session import config

    cfg = config(tmp_path, sources={"corpsu": tmp_path})
    with pytest.raises(StoreError) as e:
        source_reader(cfg, load_definitions(cfg))
    assert e.value.code == "source_unknown" and "corpsu" in e.value.message


def test_extension_systems_are_built_once_by_full_name_and_a_given_one_wins(
    tmp_path: Path,
) -> None:
    from aksha.app import extension_systems, load_definitions
    from tests.support.session import config

    cfg = config(tmp_path)
    loaded = load_definitions(cfg)
    built = extension_systems(cfg, loaded)
    assert list(built) == ["board.ledger"]
    assert built["board.ledger"].ledger_id() == "board-ledger-1"
    assert vars(built["board.ledger"])["path"].parent == cfg.system_dir("board.ledger")
    assert extension_systems(cfg, loaded, skip={"board.ledger"}) == {}


def test_a_factory_that_builds_no_system_of_record_is_refused(tmp_path: Path) -> None:
    from dataclasses import replace

    from aksha.app import extension_systems, load_definitions
    from aksha.runtime.api import SystemOfRecordSpec
    from tests.support.session import config

    cfg = config(tmp_path)
    (scribe,) = load_definitions(cfg)
    (board,) = scribe.extensions
    broken = replace(board, ext=replace(board.ext, systems=(
        SystemOfRecordSpec("ledger", lambda *, state_dir: object()),)))  # fmt: skip
    with pytest.raises(StoreError) as e:
        extension_systems(cfg, [replace(scribe, extensions=(broken,))])
    assert e.value.code == "system_invalid"
