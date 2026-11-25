"""Parametrise the contract over every storage adapter: ``backend`` and a writer ``store``."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.runtime.events.catalogue import Catalogue
from aksha.runtime.storage.ports import EventStore
from tests.support.backends import Backend, PostgresBackend, SQLiteBackend


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Backend]:
    if request.param == "sqlite":
        yield SQLiteBackend(tmp_path)
        return
    dsn = request.getfixturevalue("postgres_dsn")
    yield PostgresBackend(dsn)


@pytest.fixture
def store(backend: Backend, clock: FakeClock, catalogue: Catalogue) -> Iterator[EventStore]:
    s = backend.open(clock, writer=True, catalogue=catalogue)
    yield s
    s.close()
