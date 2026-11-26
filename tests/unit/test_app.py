import os
from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import SQLiteEventStore
from aksha.app import StorageConfig, data_home, open_event_store, runtime_commit
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


def test_open_event_store_opens_sqlite_by_default(tmp_path: Path, clock: FakeClock) -> None:
    store = open_event_store(
        StorageConfig.from_env({"AKSHA_HOME": str(tmp_path / "home")}), clock, writer=True
    )
    try:
        assert isinstance(store, SQLiteEventStore)
        assert (tmp_path / "home" / "aksha.sqlite").exists()
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
    assert StorageConfig.from_env({}).blob_path == data_home({}) / "blobs"
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


def test_state_lives_under_aksha_home_or_the_platform_data_folder(tmp_path: Path) -> None:
    home = Path("/h")
    assert data_home({"AKSHA_HOME": "state"}) == Path("state")
    assert data_home({}, platform="linux", home=home) == home / ".local/share/aksha"
    assert data_home({"XDG_DATA_HOME": "/x"}, platform="linux", home=home) == Path("/x/aksha")
    # A relative XDG_DATA_HOME is invalid by the XDG spec and is ignored.
    assert data_home({"XDG_DATA_HOME": "x"}, platform="linux", home=home) == (
        home / ".local/share/aksha"
    )
    assert data_home({}, platform="darwin", home=home) == home / "Library/Application Support/aksha"
    assert data_home({"LOCALAPPDATA": "/l"}, platform="win32", home=home) == Path("/l/aksha")
    assert data_home({}, platform="win32", home=home) == home / "AppData/Local/aksha"
    cfg = StorageConfig.from_env({"AKSHA_HOME": str(tmp_path)})
    assert (cfg.sqlite_path, cfg.blob_path) == (tmp_path / "aksha.sqlite", tmp_path / "blobs")
    pinned = StorageConfig.from_env({"AKSHA_HOME": str(tmp_path), "AKSHA_SQLITE_PATH": "a.db"})
    assert pinned.sqlite_path == Path("a.db") and pinned.blob_path == tmp_path / "blobs"


def test_runtime_commit_is_unknown_outside_a_checkout(tmp_path: Path) -> None:
    # Installed from a wheel, the folder above the package is site-packages' parent, which
    # may sit inside some other git repository: that repository's commit is not ours.
    (tmp_path / ".git").mkdir()
    assert runtime_commit(tmp_path) == ("unknown", False)
    commit, _ = runtime_commit()
    assert commit != "unknown"  # the tests run from a checkout
