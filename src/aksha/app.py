"""The composition root: the one place that picks adapters and wires them into the runtime.

It reads configuration (today: which storage adapter), builds each adapter, and hands it
to the runtime through the port it implements. The interfaces in :mod:`aksha.interfaces`
call it; nothing in :mod:`aksha.runtime` imports it. ADR-P20 decision 1 names this file.

Storage configuration, from the environment:

=====================  =====================================  =====================
Variable               Meaning                                Default
=====================  =====================================  =====================
``AKSHA_STORE``        ``sqlite`` or ``postgres``             ``sqlite``
``AKSHA_POSTGRES_DSN`` libpq DSN, needed for ``postgres``     none
``AKSHA_SQLITE_PATH``  the SQLite file                        ``var/aksha.sqlite``
``AKSHA_BLOB_PATH``    the filesystem blob store's folder     ``var/blobs``
=====================  =====================================  =====================

SQLite is the default so a run needs no setup: one file, no server (decision 34, 26 Nov
2026). PostgreSQL is the production backend (ADR-P01 D1): choose it with
``AKSHA_STORE=postgres`` and ``AKSHA_POSTGRES_DSN``. Both pass one contract suite. The
PostgreSQL driver is the optional extra ``aksha[postgres]``, imported only when PostgreSQL
is chosen, so the runtime itself stays standard library plus ``jsonschema``.

The blob store (decision 25, 26 Nov 2026) holds payload values over the 64 KiB cap and
large artifact bodies, content-addressed. The filesystem store is the only backend built;
an object-store backend for unattended runs comes later behind the same port.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from aksha.adapters.blobs.filesystem import FilesystemBlobStore
from aksha.adapters.storage import sqlite
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.events.catalogue import Catalogue
from aksha.runtime.execution.ports import Clock
from aksha.runtime.storage.errors import StoreError
from aksha.runtime.storage.ports import EventStore

Backend = Literal["sqlite", "postgres"]


@dataclass(frozen=True)
class StorageConfig:
    """Which event store backend to open, and where."""

    backend: Backend = "sqlite"
    postgres_dsn: str | None = None
    sqlite_path: Path = Path("var/aksha.sqlite")
    blob_path: Path = Path("var/blobs")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> StorageConfig:
        """Read the configuration from environment variables (see the module docstring)."""
        backend = env.get("AKSHA_STORE", "sqlite")
        if backend not in ("sqlite", "postgres"):
            raise StoreError("unknown_backend", f"AKSHA_STORE={backend!r}: use sqlite or postgres")
        return cls(
            backend=cast(Backend, backend),
            sqlite_path=Path(env.get("AKSHA_SQLITE_PATH", "var/aksha.sqlite")),
            postgres_dsn=env.get("AKSHA_POSTGRES_DSN"),
            blob_path=Path(env.get("AKSHA_BLOB_PATH", "var/blobs")),
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
            "AKSHA_STORE=postgres needs AKSHA_POSTGRES_DSN, a libpq connection string; "
            "leave AKSHA_STORE unset for the local SQLite file",
        )
    from aksha.adapters.storage import postgres

    return postgres.open_store(config.postgres_dsn, clock, writer=writer, catalogue=catalogue)


def open_blob_store(config: StorageConfig) -> BlobStore:
    """Open the blob store: a folder of content-addressed files at ``config.blob_path``."""
    return FilesystemBlobStore(config.blob_path)
