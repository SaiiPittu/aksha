"""PostgreSQL-only behaviour; the shared rules are in ``tests/contract/storage``."""

from __future__ import annotations

import builtins
import threading
import uuid
from typing import Any

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage import postgres
from aksha.runtime.events.catalogue import Catalogue
from aksha.runtime.storage.errors import StoreError
from tests.support.events import probe
from tests.support.postgres import REQUIRE_ENV, PostgresServer, skip_or_fail


def test_missing_driver_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def refuse(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "psycopg" or name.startswith("psycopg."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    with pytest.raises(StoreError) as e:
        postgres.driver()
    assert e.value.code == "driver_missing"
    assert "aksha[postgres]" in str(e.value)


def test_writer_holds_the_advisory_lock(
    postgres_dsn: str, clock: FakeClock, catalogue: Catalogue
) -> None:
    import psycopg

    store = postgres.open_store(postgres_dsn, clock, writer=True, catalogue=catalogue)
    try:
        with psycopg.connect(postgres_dsn, autocommit=True) as conn:
            row = conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
                "AND objid = %s",
                (postgres.WRITER_LOCK_KEY,),
            ).fetchone()
        assert row is not None and row[0] == 1
    finally:
        store.close()


def test_reader_session_is_read_only(
    postgres_dsn: str, clock: FakeClock, catalogue: Catalogue
) -> None:
    writer = postgres.open_store(postgres_dsn, clock, writer=True, catalogue=catalogue)
    writer.append("o1", "U2", [probe(1)])
    reader = postgres.open_store(postgres_dsn, clock, writer=False, catalogue=catalogue)
    try:
        assert reader.setting("default_transaction_read_only") == "on"
        assert writer.setting("default_transaction_read_only") == "off"
    finally:
        reader.close()
        writer.close()


def test_reader_needs_the_schema(postgres_dsn: str, clock: FakeClock) -> None:
    with pytest.raises(StoreError) as e:
        postgres.open_store(postgres_dsn, clock, writer=False)
    assert e.value.code == "store_missing"


def test_unreachable_server_is_a_store_error(clock: FakeClock) -> None:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        skip_or_fail("psycopg is missing (pip install 'aksha[postgres]')")
    dsn = "host=127.0.0.1 port=1 user=nobody dbname=none connect_timeout=1"
    with pytest.raises(StoreError) as e:
        postgres.open_store(dsn, clock, writer=False)
    assert e.value.code == "store_unreachable"


def test_require_flag_turns_a_skip_into_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        skip_or_fail("no server")
    monkeypatch.setenv(REQUIRE_ENV, "1")
    with pytest.raises(pytest.fail.Exception, match="no server"):
        skip_or_fail("no server")


def test_append_waits_for_the_transaction_lock(
    postgres_dsn: str, clock: FakeClock, catalogue: Catalogue
) -> None:
    """A second writer that slipped past the session lock still serialises (H5).

    The store's session lock is released by hand to stand for that slip; a raw session
    then holds ``pg_advisory_xact_lock`` and the append must wait until it commits.
    """
    import psycopg

    store = postgres.open_store(postgres_dsn, clock, writer=True, catalogue=catalogue)
    store.append("o1", "U2", [probe(1)])
    store._db().execute("SELECT pg_advisory_unlock(%s)", (postgres.WRITER_LOCK_KEY,))
    raw = psycopg.connect(postgres_dsn)  # not autocommit: the lock lasts until commit
    done = threading.Event()
    appended: list[int] = []

    def append() -> None:
        appended.extend(e.seq for e in store.append("o1", "U2", [probe(2)]))
        done.set()

    try:
        raw.execute("SELECT pg_advisory_xact_lock(%s)", (postgres.WRITER_LOCK_KEY,))
        worker = threading.Thread(target=append, daemon=True)
        worker.start()
        assert not done.wait(0.3), "append ran while another session held the xact lock"
        raw.commit()
        assert done.wait(10)
        worker.join(10)
        assert appended == [2]
        assert store.verify() is None
    finally:
        raw.close()
        store.close()


def test_a_database_that_is_not_utf8_is_refused(
    postgres_server: PostgresServer, clock: FakeClock
) -> None:
    import psycopg

    name = f"aksha_t_ascii_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(postgres_server.admin_dsn, autocommit=True) as conn:
        conn.execute(
            f"CREATE DATABASE \"{name}\" ENCODING 'SQL_ASCII' LC_COLLATE 'C' LC_CTYPE 'C'"
            " TEMPLATE template0"
        )
    try:
        for writer in (True, False):
            with pytest.raises(StoreError) as e:
                postgres.open_store(postgres_server.dsn(name), clock, writer=writer)
            assert e.value.code == "encoding_unsupported"
    finally:
        postgres_server.drop_database(name)
