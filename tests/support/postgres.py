"""An ephemeral PostgreSQL server for the contract suite: one labelled docker container.

The container is named ``aksha-pg-test`` and labelled ``aksha.test=contract``. It is started
on a free local port and removed when the test session ends. Nothing here touches another
container: a container with the same name but without the label makes the suite skip. The
image is never pulled (``--pull=never``), so the suite needs no network; set
``AKSHA_PG_IMAGE`` to use another local image, or ``AKSHA_TEST_POSTGRES_DSN`` to use a
server you already run instead of docker.

With ``AKSHA_REQUIRE_POSTGRES=1`` (``make test-postgres``), a reason to skip is a failure
instead, so a run that was meant to prove the PostgreSQL adapter cannot pass by skipping it.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any

CONTAINER = "aksha-pg-test"
LABEL = "aksha.test=contract"
IMAGE = os.environ.get("AKSHA_PG_IMAGE", "postgres:16")
READY_TIMEOUT_S = 60.0


REQUIRE_ENV = "AKSHA_REQUIRE_POSTGRES"


def skip_or_fail(reason: str) -> None:
    """Skip the test with ``reason``, or fail it when :data:`REQUIRE_ENV` is ``1``."""
    import pytest

    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(f"{REQUIRE_ENV}=1, but {reason}")
    pytest.skip(reason)


@dataclass(frozen=True)
class PostgresServer:
    """A running server the tests may create and drop databases on."""

    admin_dsn: str
    owned_container: bool

    def dsn(self, database: str) -> str:
        """The DSN of ``database`` on this server."""
        return f"{self.admin_dsn} dbname={database}"

    def create_database(self) -> str:
        """Create a fresh, uniquely named database and return its name."""
        name = f"aksha_t_{uuid.uuid4().hex[:10]}"
        with _connect(self.admin_dsn) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        return name

    def drop_database(self, name: str) -> None:
        """Drop ``name``, ending any session still connected to it."""
        with _connect(self.admin_dsn) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _connect(dsn: str) -> Any:
    import psycopg

    return psycopg.connect(dsn, autocommit=True, connect_timeout=2)


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(dsn: str) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while True:
        try:
            with _connect(dsn) as conn:
                conn.execute("SELECT 1")
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.3)


def start_server() -> PostgresServer | str:
    """Start (or find) a server; return it, or a reason to skip the PostgreSQL tests."""
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return "PostgreSQL adapter not tested: psycopg is missing (pip install 'aksha[postgres]')"
    existing = os.environ.get("AKSHA_TEST_POSTGRES_DSN")
    if existing:
        _wait_ready(existing)
        return PostgresServer(admin_dsn=existing, owned_container=False)
    if shutil.which("docker") is None:
        return "PostgreSQL adapter not tested: docker is not installed"
    if _docker("info", "--format", "{{.ServerVersion}}").returncode != 0:
        return "PostgreSQL adapter not tested: the docker daemon is not reachable"
    if _docker("image", "inspect", IMAGE).returncode != 0:
        return f"PostgreSQL adapter not tested: image {IMAGE} is not present locally (no pull)"
    found = _docker("ps", "-a", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Labels}}")
    if found.stdout.strip():
        if LABEL not in found.stdout:
            return f"PostgreSQL adapter not tested: a container named {CONTAINER} is not ours"
        _docker("rm", "-f", CONTAINER)  # a leftover of an interrupted run of this suite
    port = _free_port()
    run = _docker(
        "run", "-d", "--rm", "--pull=never", "--name", CONTAINER, "--label", LABEL,
        "-e", "POSTGRES_USER=aksha", "-e", "POSTGRES_PASSWORD=aksha",
        "-p", f"127.0.0.1:{port}:5432", IMAGE, "-c", "fsync=off",
    )  # fmt: skip
    if run.returncode != 0:
        return f"PostgreSQL adapter not tested: docker run failed: {run.stderr.strip()}"
    dsn = f"host=127.0.0.1 port={port} user=aksha password=aksha"
    try:
        _wait_ready(f"{dsn} dbname=postgres")
    except Exception:
        stop_server(PostgresServer(admin_dsn=dsn, owned_container=True))
        raise
    return PostgresServer(admin_dsn=f"{dsn} dbname=postgres", owned_container=True)


def stop_server(server: PostgresServer) -> None:
    """Remove the container if this suite started it; leave any other server alone."""
    if server.owned_container:
        _docker("rm", "-f", CONTAINER)
