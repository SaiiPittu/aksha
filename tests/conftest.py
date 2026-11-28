"""Fixtures every test folder shares: a fake clock, the test catalogue and a PostgreSQL server.

The PostgreSQL server starts only when a test asks for ``postgres_dsn``, once per run (the
workers of a pytest-xdist run share it), and is removed at the end (see
``tests/support/postgres.py``). Without psycopg or docker those tests skip and say why,
unless ``AKSHA_REQUIRE_POSTGRES=1`` makes that a failure.
"""

from __future__ import annotations

import hashlib
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


def pytest_addoption(parser: pytest.Parser) -> None:
    """``--shard K/N``: run only the K-th of N slices of the suite (CI runs them side by side)."""
    parser.addoption(
        "--shard",
        default=None,
        metavar="K/N",
        help="run only shard K of N (1-based); see shard_of in tests/conftest.py",
    )


# The slowest test files, heaviest first (summed test time of a full run, 28 Nov 2026). A
# hash alone put the three longest (each a whole episode or eval suite behind one shared
# fixture) in the same CI shard, so these are dealt out instead: in this order, to shards
# 1, 2, ..., N, then N, ..., 1, and so on. Every other test is placed by its hash. Re-measure
# with `make test PYTEST_PARALLEL="-n auto --dist loadgroup --durations=0"` when one shard
# runs much longer than the rest; tests/test_shards.py checks that every path here exists.
SHARD_PINNED_FILES = (
    "tests/integration/test_eval_portfolio.py",
    "tests/integration/test_http_reads.py",
    "tests/integration/test_demo.py",
    "tests/integration/test_uploads.py",
    "tests/integration/test_eval_agents.py",
    "tests/agents/test_patent_episodes.py",
    "tests/integration/test_resume.py",
    "tests/integration/test_corrections.py",
    "tests/unit/extensions/ingest/test_parser_worker.py",
    "tests/integration/test_reuse.py",
    "tests/integration/test_ingest_uploads.py",
    "tests/integration/test_portfolio.py",
)


def shard_key(item: pytest.Item) -> str:
    """What a test is hashed by: its ``xdist_group`` when it has one, else its node ID.

    Tests that share a module- or class-scoped fixture carry one group (see
    ``pytest_collection_modifyitems``), so they land in one shard and the fixture is still
    set up once; every other test is placed on its own, which spreads a file's tests across
    the shards instead of piling them into one.
    """
    marker = item.get_closest_marker("xdist_group")
    return str(marker.args[0]) if marker is not None and marker.args else item.nodeid


def shard_of(nodeid: str, key: str, count: int) -> int:
    """The 1-based shard of a test, from nothing but its own node ID and key.

    A test in a pinned file goes where the deal in ``SHARD_PINNED_FILES`` puts it; any other
    goes by a stable hash of its key. Either way the answer depends on that one test, not on
    what else was collected or on timing data, so the same test is in the same shard on every
    machine and run, the shards never overlap, and together they are the whole suite.
    """
    path = nodeid.split("::", 1)[0]
    if path in SHARD_PINNED_FILES:
        position = SHARD_PINNED_FILES.index(path)
        lap, slot = divmod(position, count)
        return (count - slot) if lap % 2 else (slot + 1)
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:8], "big") % count + 1


def _parse_shard(value: str) -> tuple[int, int]:
    index, _, count = value.partition("/")
    try:
        k, n = int(index), int(count)
    except ValueError:
        raise pytest.UsageError(f"--shard wants K/N, such as 1/4, not {value!r}") from None
    if not 1 <= k <= n:
        raise pytest.UsageError(f"--shard {value}: K must be between 1 and N")
    return k, n


def pytest_configure(config: pytest.Config) -> None:
    """Declare ``xdist_group`` even without pytest-xdist (``make test-serial``); check --shard."""
    config.addinivalue_line("markers", "xdist_group(name): run these tests on one worker")
    shard = config.getoption("--shard")
    if shard is not None:
        _parse_shard(shard)  # a bad --shard fails before collection, not after it


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep the tests of each shared fixture on one pytest-xdist worker (``--dist loadgroup``).

    A module- or class-scoped fixture (a whole episode, run once for many assertions) is set
    up once on every worker that runs a test using it. So the tests using one are grouped
    by module, fixture and parameter (``sent[stillroom]`` and ``sent[stillroom-v2]`` are two
    groups and may run side by side); every other test goes to whichever worker is free.
    With ``--shard K/N``, every test outside shard K is then deselected.
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

    shard = config.getoption("--shard")
    if shard is not None:
        index, count = _parse_shard(shard)
        kept: list[pytest.Item] = []
        dropped: list[pytest.Item] = []
        for item in items:
            mine = shard_of(item.nodeid, shard_key(item), count) == index
            (kept if mine else dropped).append(item)
        if dropped:
            config.hook.pytest_deselected(items=dropped)
        items[:] = kept
