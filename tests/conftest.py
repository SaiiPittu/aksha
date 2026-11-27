"""Fixtures every test folder shares: a fake clock, the test catalogue and a PostgreSQL server.

The PostgreSQL server starts only when a test asks for ``postgres_dsn``, once per run (the
workers of a pytest-xdist run share it), and is removed at the end (see
``tests/support/postgres.py``). Without psycopg or docker those tests skip and say why,
unless ``AKSHA_REQUIRE_POSTGRES=1`` makes that a failure.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.runtime.events.catalogue import Catalogue
from tests.support.events import START, probe_catalogue
from tests.support.postgres import (
    PostgresServer,
    release_shared,
    shared_server,
    skip_or_fail,
    start_server,
    stop_server,
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def catalogue() -> Catalogue:
    return probe_catalogue()


@pytest.fixture(scope="session")
def postgres_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PostgresServer]:
    """One server per test run; under pytest-xdist its workers share it."""
    run_id = os.environ.get("PYTEST_XDIST_TESTRUNUID")
    root = tmp_path_factory.getbasetemp().parent  # the directory every worker shares
    server = start_server() if run_id is None else shared_server(root, run_id)
    if isinstance(server, str):
        skip_or_fail(server)
        raise AssertionError("unreachable")  # skip_or_fail always raises
    try:
        yield server
    finally:
        if run_id is None:
            stop_server(server)
        else:
            release_shared(root, server)


@pytest.fixture
def postgres_dsn(postgres_server: PostgresServer) -> Iterator[str]:
    """A DSN of a fresh, empty database, dropped after the test."""
    name = postgres_server.create_database()
    try:
        yield postgres_server.dsn(name)
    finally:
        postgres_server.drop_database(name)


def pytest_configure(config: pytest.Config) -> None:
    """Declare ``xdist_group`` even when pytest-xdist is off (``make test-serial``)."""
    config.addinivalue_line("markers", "xdist_group(name): run these tests on one worker")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep the tests of each shared fixture on one pytest-xdist worker (``--dist loadgroup``).

    A module- or class-scoped fixture (a whole episode, run once for many assertions) is set
    up once on every worker that runs a test using it. So the tests using one are grouped
    by module, fixture and parameter (``sent[stillroom]`` and ``sent[stillroom-v2]`` are two
    groups and may run side by side); every other test goes to whichever worker is free.
    """
    for item in items:
        info = getattr(item, "_fixtureinfo", None)
        if info is None:
            continue
        params = getattr(getattr(item, "callspec", None), "params", {})
        shared = sorted(
            f"{name}[{params[name]}]" if name in params else name
            for name, defs in info.name2fixturedefs.items()
            if defs and defs[-1].scope in ("module", "class", "package")
        )
        if shared:
            module = item.nodeid.split("::", 1)[0]
            item.add_marker(pytest.mark.xdist_group(f"{module}::{'+'.join(shared)}"))
