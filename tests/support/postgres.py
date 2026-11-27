"""An ephemeral PostgreSQL server for the contract suite: one labelled docker container.

The container is named ``aksha-pg-test-<8 hex digits>``, unique to the test session, and
labelled ``aksha.test=contract``. It is started on a free local port and removed when the
test session ends. Nothing here touches another container, so two suites running at once
(in two worktrees, or by two agents) each keep their own server until they finish. Under
pytest-xdist the workers of one run share one server (:func:`shared_server`): the first
worker to need it starts it, each takes a reference, and the last to finish removes it;
every test still gets its own database. The
image is never pulled (``--pull=never``), so the suite needs no network; set
``AKSHA_PG_IMAGE`` to use another local image, or ``AKSHA_TEST_POSTGRES_DSN`` to use a
server you already run instead of docker.

With ``AKSHA_REQUIRE_POSTGRES=1`` (``make test-postgres``), a reason to skip is a failure
instead, so a run that was meant to prove the PostgreSQL adapter cannot pass by skipping it.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONTAINER = f"aksha-pg-test-{uuid.uuid4().hex[:8]}"
"""This session's container name: removing it never ends another session's server."""
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
    container: str = CONTAINER
    """The docker container this suite started, when ``owned_container``."""

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


def start_server(container: str = CONTAINER) -> PostgresServer | str:
    """Start (or find) a server; return it, or a reason to skip the PostgreSQL tests."""
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return (
            "PostgreSQL adapter not tested: psycopg is missing "
            "(pip install 'aksha-harness[postgres]')"
        )
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
    found = _docker("ps", "-a", "--filter", f"name=^{container}$", "--format", "{{.Labels}}")
    if found.stdout.strip():
        return f"PostgreSQL adapter not tested: a container named {container} already exists"
    port = _free_port()
    run = _docker(
        "run", "-d", "--rm", "--pull=never", "--name", container, "--label", LABEL,
        "-e", "POSTGRES_USER=aksha", "-e", "POSTGRES_PASSWORD=aksha",
        # Data on a tmpfs: the image declares a VOLUME for PGDATA, so without this every
        # run (and every killed run) leaves an anonymous Docker volume behind.
        "--tmpfs", "/var/lib/postgresql/data:rw",
        "-p", f"127.0.0.1:{port}:5432", IMAGE, "-c", "fsync=off",
    )  # fmt: skip
    if run.returncode != 0:
        return f"PostgreSQL adapter not tested: docker run failed: {run.stderr.strip()}"
    dsn = f"host=127.0.0.1 port={port} user=aksha password=aksha"
    try:
        _wait_ready(f"{dsn} dbname=postgres")
    except Exception:
        stop_server(PostgresServer(admin_dsn=dsn, owned_container=True, container=container))
        raise
    return PostgresServer(
        admin_dsn=f"{dsn} dbname=postgres", owned_container=True, container=container
    )


def stop_server(server: PostgresServer) -> None:
    """Remove the container if this suite started it; leave any other server alone."""
    if server.owned_container:
        _docker("rm", "-f", "-v", server.container)


@contextmanager
def _locked(root: Path) -> Iterator[Path]:
    """Hold the run-wide lock on ``root`` (shared by every xdist worker of one run)."""
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "postgres.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield root / "postgres.json"
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def shared_server(root: Path, run_id: str) -> PostgresServer | str:
    """The server every worker of run ``run_id`` shares; start it if no worker has yet.

    ``root`` is a directory all the run's workers see. Each call takes one reference;
    :func:`release_shared` gives it back, and the last reference removes the container.
    """
    with _locked(root) as state:
        if state.exists():
            data = json.loads(state.read_text(encoding="utf-8"))
            data["refs"] += 1
            state.write_text(json.dumps(data), encoding="utf-8")
            return PostgresServer(data["admin_dsn"], data["owned"], data["container"])
        server = start_server(f"aksha-pg-test-{run_id[:8]}")
        if isinstance(server, PostgresServer):
            data = {"admin_dsn": server.admin_dsn, "owned": server.owned_container,
                    "container": server.container, "refs": 1}  # fmt: skip
            state.write_text(json.dumps(data), encoding="utf-8")
        return server


def release_shared(root: Path, server: PostgresServer) -> None:
    """Give back one reference to the shared server; the last one removes it."""
    with _locked(root) as state:
        data = json.loads(state.read_text(encoding="utf-8"))
        data["refs"] -= 1
        if data["refs"] > 0:
            state.write_text(json.dumps(data), encoding="utf-8")
            return
        state.unlink()
        stop_server(server)
