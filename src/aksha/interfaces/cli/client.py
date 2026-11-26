"""The CLI's client of a running server (server mode, decision 22): JSON over HTTP, stdlib only.

A CLI command finds the server from ``AKSHA_SERVER_URL`` or the server file ``serve``
writes beside the store, and checks it answers ``/health``; with no server answering, the
command runs embedded. Every write is one ``POST /api/v1/commands``, so the server's
runtime thread stays the only writer.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aksha.interfaces import wire
from aksha.runtime.session.named import NamedCommand
from aksha.runtime.session.types import CommandReceipt
from aksha.runtime.session.views import RunStatus

HEALTH_TIMEOUT_S = 2.0
POLL_S = 0.1


class ServerError(Exception):
    """The server answered with an error body: its HTTP status, code and message."""

    def __init__(self, status: int, code: str, message: str, details: object = None) -> None:
        """Hold the error the server sent."""
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def find_server(url: str | None, server_file: Path) -> str | None:
    """The URL of a server that answers ``/health``, or None (then the CLI runs embedded)."""
    if url is None and server_file.is_file():
        try:
            url = str(json.loads(server_file.read_text(encoding="utf-8"))["url"])
        except (ValueError, KeyError, OSError):
            url = None
    if url is None:
        return None
    try:
        Client(url, "health").get("/health", timeout=HEALTH_TIMEOUT_S)
    except (OSError, ServerError):
        return None
    return url


@dataclass
class Client:
    """One owner's calls to the server at ``url``."""

    url: str
    owner: str

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None,
              timeout: float) -> dict[str, Any]:  # fmt: skip
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.url.rstrip("/") + "/api/v1" + path, data=data, method=method,
            headers={"Content-Type": "application/json", "X-Aksha-User": self.owner},
        )  # fmt: skip
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                loaded: dict[str, Any] = json.loads(response.read() or b"{}")
                return loaded
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}").get("error") or {}
            except ValueError:
                err = {}
            raise ServerError(
                e.code,
                str(err.get("code", "http_error")),
                str(err.get("message", e.reason)),
                err.get("details"),
            ) from None

    def get(self, path: str, *, timeout: float = 30.0) -> dict[str, Any]:
        """``GET`` a JSON read."""
        return self._call("GET", path, None, timeout)

    def dispatch(self, command: NamedCommand) -> CommandReceipt:
        """Send one command; a refusal comes back as its receipt, not as an error."""
        try:
            data = self._call("POST", "/commands", wire.command_to_json(command), 90.0)
        except ServerError as e:
            if isinstance(e.details, Mapping) and "command_id" in e.details:
                return wire.receipt_from_json(e.details)
            raise
        return wire.receipt_from_json(data["receipt"])

    def run(self, run: str) -> RunStatus | None:
        """One run's status, or None if the owner has no such run."""
        for row in self.get("/runs")["runs"]:
            if row["run"] == run:
                return wire.run_from_json(row)
        return None

    def wait_for_yield(self, run: str, timeout_s: float | None = None) -> RunStatus | None:
        """Poll until ``run`` waits on a person or ends (ADR-P19 D3), or the timeout passes."""
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            status = self.run(run)
            if status is None or status.state != "running":
                return status
            if deadline is not None and time.monotonic() > deadline:
                return status
            time.sleep(POLL_S)
