"""The composition root: the one place that picks adapters and wires them into the runtime.

It reads configuration (today: which storage adapter), builds each adapter, and hands it
to the runtime through the port it implements. The interfaces in :mod:`aksha.interfaces`
call it; nothing in :mod:`aksha.runtime` imports it. ADR-P20 decision 1 names this file.

Storage configuration, from the environment:

=====================  =====================================  =====================
Variable               Meaning                                Default
=====================  =====================================  =====================
``AKSHA_STORE``        ``postgres`` or ``sqlite``             ``postgres``
``AKSHA_POSTGRES_DSN`` a libpq connection string              none (required)
``AKSHA_SQLITE_PATH``  the SQLite file                        ``var/aksha.sqlite``
=====================  =====================================  =====================

PostgreSQL is the default because it is the production backend (ADR-P01 D1, amended 25
Nov 2026). SQLite is the zero-setup backend for local and offline runs and the tests:
choose it with ``AKSHA_STORE=sqlite``. Both pass one contract suite. The PostgreSQL driver
is the optional extra ``aksha[postgres]``, imported only when PostgreSQL is chosen, so the
runtime itself stays standard library plus ``jsonschema``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from aksha.adapters.storage import sqlite
from aksha.runtime.events.catalogue import Catalogue
from aksha.runtime.execution.ports import Clock
from aksha.runtime.storage.errors import StoreError
from aksha.runtime.storage.ports import EventStore

Backend = Literal["sqlite", "postgres"]


@dataclass(frozen=True)
class StorageConfig:
    """Which event store backend to open, and where."""

    backend: Backend = "postgres"
    postgres_dsn: str | None = None
    sqlite_path: Path = Path("var/aksha.sqlite")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> StorageConfig:
        """Read the configuration from environment variables (see the module docstring)."""
        backend = env.get("AKSHA_STORE", "postgres")
        if backend not in ("sqlite", "postgres"):
            raise StoreError("unknown_backend", f"AKSHA_STORE={backend!r}: use postgres or sqlite")
        return cls(
            backend=cast(Backend, backend),
            sqlite_path=Path(env.get("AKSHA_SQLITE_PATH", "var/aksha.sqlite")),
            postgres_dsn=env.get("AKSHA_POSTGRES_DSN"),
        )


def open_event_store(
    config: StorageConfig,
    clock: Clock,
    *,
    writer: bool,
    catalogue: Catalogue | None = None,
) -> EventStore:
    """Open the configured event store. The PostgreSQL driver is imported only if chosen."""
    if config.backend == "sqlite":
        return sqlite.open_store(config.sqlite_path, clock, writer=writer, catalogue=catalogue)
    if not config.postgres_dsn:
        raise StoreError(
            "dsn_required",
            "the event store is PostgreSQL by default: set AKSHA_POSTGRES_DSN, "
            "or AKSHA_STORE=sqlite for a local file",
        )
    from aksha.adapters.storage import postgres

    return postgres.open_store(config.postgres_dsn, clock, writer=writer, catalogue=catalogue)
