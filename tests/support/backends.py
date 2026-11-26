"""One harness per storage adapter, so a contract test is written once and run against each.

A harness opens stores on a fresh, empty backend, runs raw SQL past the adapter (to show
the database itself refuses an edit), and writes the child script the lock test kills.
"""

from __future__ import annotations

import sqlite3
import textwrap
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from aksha.adapters.storage import postgres, sqlite
from aksha.runtime.events.catalogue import Catalogue
from aksha.runtime.execution.ports import Clock
from aksha.runtime.storage.ports import EventStore

TAMPERED_TABLES = ("events", "artifacts", "store_meta", "erasables")
"""The tables :meth:`Backend.tamper` opens to edits."""


class Backend(ABC):
    """A fresh, empty event store location for one adapter."""

    name: str
    destructive_sql: tuple[str, ...] = (
        "UPDATE events SET type = 'x'",
        "DELETE FROM events",
        "UPDATE artifacts SET kind = 'x'",
        "DELETE FROM artifacts",
    )

    @abstractmethod
    def open(
        self, clock: Clock, *, writer: bool, catalogue: Catalogue, store_id: str | None = None
    ) -> EventStore:
        """Open a store at this location; ``store_id`` names a store this call creates."""

    @abstractmethod
    def tamper(self, *sql: str) -> None:
        """Past the adapter, turn off the append-only triggers and run ``sql``.

        The triggers on ``events``, ``artifacts``, ``store_meta`` and ``erasables`` are
        turned off.

        This is what someone with direct database access can do; the chain tests use it to
        show that ``verify()`` finds the change.
        """

    @abstractmethod
    def rows(self, sql: str) -> list[tuple[Any, ...]]:
        """Run a read-only query on a raw connection and return its rows."""

    @abstractmethod
    def refusal(self, sql: str) -> str:
        """Run ``sql`` on a raw connection and return the database's error message."""

    @abstractmethod
    def writer_child_source(self) -> str:
        """Python source that opens a writer, prints ``locked`` and sleeps."""

    @abstractmethod
    def set_schema_version(self, version: int | None) -> None:
        """Past the adapter, record ``version`` in ``meta``, or drop ``meta`` when None.

        This is how a test makes a store of another shape: the append-only triggers on
        ``meta`` are removed first, which only a raw connection can do.
        """


class SQLiteBackend(Backend):
    """A SQLite file in a temporary folder."""

    name = "sqlite"

    def __init__(self, folder: Path) -> None:
        self.path = folder / "var" / "aksha.sqlite"

    def open(
        self, clock: Clock, *, writer: bool, catalogue: Catalogue, store_id: str | None = None
    ) -> EventStore:
        return sqlite.open_store(
            self.path, clock, writer=writer, catalogue=catalogue, store_id=store_id
        )

    def tamper(self, *sql: str) -> None:
        raw = sqlite3.connect(self.path)
        try:
            for table in TAMPERED_TABLES:
                raw.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")
                raw.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")
            for statement in sql:
                raw.execute(statement)
            raw.commit()
        finally:
            raw.close()

    def rows(self, sql: str) -> list[tuple[Any, ...]]:
        raw = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return raw.execute(sql).fetchall()
        finally:
            raw.close()

    def refusal(self, sql: str) -> str:
        raw = sqlite3.connect(self.path)
        try:
            raw.execute(sql)
        except sqlite3.DatabaseError as e:
            return str(e)
        finally:
            raw.close()
        raise AssertionError(f"SQLite accepted {sql!r}")

    def writer_child_source(self) -> str:
        return textwrap.dedent(
            f"""
            import time
            from datetime import UTC, datetime
            from pathlib import Path
            from aksha.adapters.clock.fake import FakeClock
            from aksha.adapters.storage.sqlite import open_store
            s = open_store(Path({str(self.path)!r}),
                           FakeClock(datetime(2026, 1, 1, tzinfo=UTC)), writer=True)
            print("locked", flush=True)
            time.sleep(60)
            """
        )

    def set_schema_version(self, version: int | None) -> None:
        raw = sqlite3.connect(self.path)
        try:
            raw.execute("DROP TRIGGER meta_no_update")
            raw.execute("DROP TRIGGER meta_no_delete")
            if version is None:
                raw.execute("DROP TABLE meta")
            else:
                raw.execute("UPDATE meta SET schema_version = ?", (version,))
            raw.commit()
        finally:
            raw.close()


class PostgresBackend(Backend):
    """A fresh database on the test PostgreSQL server."""

    name = "postgres"
    destructive_sql = (
        *Backend.destructive_sql,
        "TRUNCATE events CASCADE",
        "TRUNCATE artifacts",
        "TRUNCATE boots",
        "TRUNCATE erasables",
    )

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def open(
        self, clock: Clock, *, writer: bool, catalogue: Catalogue, store_id: str | None = None
    ) -> EventStore:
        return postgres.open_store(
            self.dsn, clock, writer=writer, catalogue=catalogue, store_id=store_id
        )

    def tamper(self, *sql: str) -> None:
        import psycopg

        conn: Any = psycopg.connect(self.dsn, autocommit=True)
        try:
            for table in TAMPERED_TABLES:
                conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
            for statement in sql:
                conn.execute(statement)
        finally:
            conn.close()

    def rows(self, sql: str) -> list[tuple[Any, ...]]:
        import psycopg

        conn: Any = psycopg.connect(self.dsn, autocommit=True)
        try:
            return [tuple(r) for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()

    def refusal(self, sql: str) -> str:
        import psycopg

        conn: Any = psycopg.connect(self.dsn, autocommit=True)
        try:
            conn.execute(sql)
        except psycopg.Error as e:
            return str(e)
        finally:
            conn.close()
        raise AssertionError(f"PostgreSQL accepted {sql!r}")

    def writer_child_source(self) -> str:
        return textwrap.dedent(
            f"""
            import time
            from datetime import UTC, datetime
            from aksha.adapters.clock.fake import FakeClock
            from aksha.adapters.storage.postgres import open_store
            s = open_store({self.dsn!r},
                           FakeClock(datetime(2026, 1, 1, tzinfo=UTC)), writer=True)
            print("locked", flush=True)
            time.sleep(60)
            """
        )

    def set_schema_version(self, version: int | None) -> None:
        import psycopg

        conn: Any = psycopg.connect(self.dsn, autocommit=True)
        try:
            if version is None:
                conn.execute("DROP TABLE meta")
            else:
                conn.execute("ALTER TABLE meta DISABLE TRIGGER USER")
                conn.execute("UPDATE meta SET schema_version = %s", (version,))
        finally:
            conn.close()
