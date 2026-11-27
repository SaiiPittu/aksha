"""Find the server the chat talks to, or start one for the chat's lifetime (local mode).

``AKSHA_SERVER_URL`` names a server to use (it must answer); otherwise the server file
``aksha serve`` writes beside the store names a running one. With neither, the chat starts
``aksha serve`` as a child process on a free loopback port and stops it on exit: a
loopback server with no accounts runs in local mode, so no sign-in is needed. A remote
server with accounts takes ``AKSHA_TOKEN`` as a bearer token.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from aksha.interfaces.cli.client import find_server

START_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 10.0
GRACE_S = 1.0


class ConnectError(Exception):
    """No server could be reached or started."""


@dataclass
class Connection:
    """The server's URL, and the child process when the chat started it."""

    url: str
    process: subprocess.Popen[bytes] | None = None
    log: IO[bytes] | None = field(default=None, repr=False)

    @property
    def started_here(self) -> bool:
        """Whether this chat started the server (and so stops it)."""
        return self.process is not None

    def close(self, *, wait: bool = True) -> None:
        """Stop the server this chat started: interrupts, so it removes its own file.

        ``wait=False`` returns after the grace period: the server, in its own process
        session, finishes shutting down by itself (within one stream heartbeat).
        """
        if self.process is not None and self.process.poll() is None:
            # The first interrupt shuts down gracefully, waiting for open responses; a
            # stream's response ends only at its next heartbeat, so a second interrupt
            # makes the server stop waiting (it still runs its clean-up and removes its file).
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(GRACE_S)
            except subprocess.TimeoutExpired:
                self.process.send_signal(signal.SIGINT)
            if not wait:
                self._close_log()
                return
            try:
                self.process.wait(STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(STOP_TIMEOUT_S)
        self._close_log()

    def _close_log(self) -> None:
        if self.log is not None:
            self.log.close()
            self.log = None


def free_port(host: str = "127.0.0.1") -> int:
    """A loopback port nothing listens on now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        port: int = s.getsockname()[1]
        return port


def connect(
    server_url: str | None, server_file: Path, env: Mapping[str, str], *, spawn: bool = True
) -> Connection:
    """The server to talk to (module doc)."""
    url = find_server(server_url, server_file)
    if url is not None:
        return Connection(url)
    if server_url is not None:
        raise ConnectError(f"no server answers at {server_url} (AKSHA_SERVER_URL)")
    if not spawn:
        raise ConnectError("no server is running; start one with: aksha serve")
    return start_server(env)


def start_server(
    env: Mapping[str, str], *, host: str = "127.0.0.1", timeout_s: float = START_TIMEOUT_S
) -> Connection:
    """Start ``aksha serve`` on a free loopback port and wait until it answers."""
    port = free_port(host)
    url = f"http://{host}:{port}"
    log = tempfile.TemporaryFile()  # noqa: SIM115 (open for the server's life)
    command = [
        sys.executable,
        "-m",
        "aksha.interfaces.cli",
        "serve",
        "--host",
        host,
        "--port",
        str(port),
    ]
    process = subprocess.Popen(
        command,
        env={**os.environ, **env},
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    connection = Connection(url, process, log)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log.seek(0)
            tail = log.read().decode("utf-8", errors="replace").strip().splitlines()[-5:]
            connection.close()
            raise ConnectError("aksha serve exited: " + (" | ".join(tail) or "no output"))
        if find_server(url, Path(os.devnull)) is not None:
            return connection
        time.sleep(0.1)
    connection.close()
    raise ConnectError(f"aksha serve did not answer at {url} within {timeout_s:.0f} s")
