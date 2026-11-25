"""Fixtures every test folder shares: a fake clock, the test catalogue and a PostgreSQL server.

The PostgreSQL server starts only when a test asks for ``postgres_dsn``, once per session,
and is removed at the end (see ``tests/support/postgres.py``). Without psycopg or docker
those tests skip and say why, unless ``AKSHA_REQUIRE_POSTGRES=1`` makes that a failure.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.runtime.events.catalogue import Catalogue
from tests.support.events import START, probe_catalogue
from tests.support.postgres import PostgresServer, skip_or_fail, start_server, stop_server


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def catalogue() -> Catalogue:
    return probe_catalogue()


@pytest.fixture(scope="session")
def postgres_server() -> Iterator[PostgresServer]:
    server = start_server()
    if isinstance(server, str):
        skip_or_fail(server)
        raise AssertionError("unreachable")  # skip_or_fail always raises
    try:
        yield server
    finally:
        stop_server(server)


@pytest.fixture
def postgres_dsn(postgres_server: PostgresServer) -> Iterator[str]:
    """A DSN of a fresh, empty database, dropped after the test."""
    name = postgres_server.create_database()
    try:
        yield postgres_server.dsn(name)
    finally:
        postgres_server.drop_database(name)
