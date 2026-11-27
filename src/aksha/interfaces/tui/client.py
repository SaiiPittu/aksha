"""The terminal UI's client of the HTTP session API: JSON commands and reads, and the SSE stream.

Standard library only, and no Textual, so it is unit-tested on its own. Every write sends
a fresh command ID (``wire.new_command_id``) and reuses it only to retry the same click
after a dropped connection, so a retry gets the first receipt back. A refusal (409, 404
with a stored receipt) comes back as a refused receipt, not an exception.

The stream (:class:`EventStream`) keeps a cursor, the ``seq`` of the last event it
yielded: a reconnect asks for ``?after=<cursor>`` (and sends ``Last-Event-ID``), so
nothing is lost or repeated, and an event at or below the cursor is dropped if a server
sends it twice. Heartbeat comments are skipped; the ``synchronized`` marker says the
replay has caught up. Reconnects back off and stop when the stop event is set.

Reads the newer reads router serves (``/sessions``, ``/artifacts``, ``/documents``,
``/inputs/upload``) are optional: a server without them answers 404, and the client says
so by returning None, or falls back (``/runs`` for sessions, ``/inputs`` for uploads).
"""

from __future__ import annotations

import base64
import contextlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aksha.interfaces import wire
from aksha.runtime.session.types import CommandReceipt

API = "/api/v1"
RETRIES = 1
"""How many times a command whose connection dropped is sent again, with the same ID."""
BACKOFF_S = (0.2, 0.5, 1.0, 2.0, 5.0)
"""The waits between stream reconnects; the last one repeats."""
STREAM_READ_TIMEOUT_S = 40.0
"""A stream read gives up after this long without a byte (the server beats every 15 s)."""


class ApiError(Exception):
    """The server answered with an error body: its HTTP status, code and message."""

    def __init__(self, status: int, code: str, message: str, details: object = None) -> None:
        """Hold the error the server sent."""
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details


# --- SSE -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SseBlock:
    """One SSE block: its ``id``, ``event`` name and ``data`` text, or a comment line."""

    id: str | None = None
    event: str | None = None
    data: str = ""
    comment: str | None = None


def parse_sse(lines: Iterable[str]) -> Iterator[SseBlock]:
    """The blocks of an SSE text, line by line (a blank line ends a block)."""
    sse_id: str | None = None
    event: str | None = None
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if sse_id is not None or event is not None or data:
                yield SseBlock(sse_id, event, "\n".join(data))
            sse_id, event, data = None, None, []
            continue
        if line.startswith(":"):
            yield SseBlock(comment=line[1:].strip())
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "id":
            sse_id = value
        elif name == "event":
            event = value
        elif name == "data":
            data.append(value)
    if sse_id is not None or event is not None or data:
        yield SseBlock(sse_id, event, "\n".join(data))


@dataclass(frozen=True)
class Synced:
    """The stream has replayed everything up to ``seq``; what follows is live."""

    seq: int


@dataclass(frozen=True)
class Resynced:
    """The server could not replay from the cursor: the consumer starts over from ``seq``."""

    seq: int


StreamItem = Mapping[str, Any] | Synced | Resynced
"""An event (the ``data`` object of an SSE event block), or a marker."""

Connect = Callable[[int | None], Iterable[str]]
"""Open the stream after a cursor (None: from the start); yields its text lines."""


@dataclass
class EventStream:
    """A session's events from ``cursor`` on, reconnecting after a drop (module doc)."""

    connect: Connect
    cursor: int | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    backoff: tuple[float, ...] = BACKOFF_S
    sleep: Callable[[float], None] = time.sleep
    synchronized: bool = False
    reconnects: int = 0
    errors: list[str] = field(default_factory=list)

    def __iter__(self) -> Iterator[StreamItem]:
        """Yield events and markers until :attr:`stop` is set."""
        attempt = 0
        while not self.stop.is_set():
            lines: Iterable[str] = ()
            try:
                lines = self.connect(self.cursor)
                for block in parse_sse(lines):
                    if self.stop.is_set():
                        return
                    item = self._item(block)
                    if item is None:
                        continue
                    attempt = 0
                    yield item
            except (OSError, ValueError, ApiError) as e:
                self.errors.append(f"{type(e).__name__}: {e}")
            finally:
                close = getattr(lines, "close", None)
                if callable(close):
                    close()
            if self.stop.is_set():
                return
            self.synchronized = False
            self.reconnects += 1
            self.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
            attempt += 1

    def _item(self, block: SseBlock) -> StreamItem | None:
        if block.comment is not None or not block.data:
            return None
        data = json.loads(block.data)
        if block.event == "synchronized":
            self.synchronized = True
            return Synced(int(data.get("seq", 0)))
        if block.event == "resync":
            self.cursor = int(data.get("seq", 0))
            return Resynced(self.cursor)
        if block.event == "transient" or not isinstance(data, Mapping):
            return None
        seq = int(data.get("seq") or (block.id or 0))
        if self.cursor is not None and seq <= self.cursor:
            return None
        self.cursor = seq
        return data


# --- the API -------------------------------------------------------------------------------


Opener = Callable[..., Any]
"""``urllib.request.urlopen`` or a test double: ``(request, timeout=...)`` to a response."""


@dataclass
class Api:
    """One owner's calls to the server at ``url`` (module doc)."""

    url: str
    owner: str
    token: str | None = field(default=None, repr=False)
    opener: Opener = urllib.request.urlopen
    timeout: float = 90.0
    _live: list[Any] = field(default_factory=list, repr=False)

    # --- transport -------------------------------------------------------------------------

    def headers(self, content_type: str | None = "application/json") -> dict[str, str]:
        """The headers of every call: the owner, and the API token when there is one."""
        out = {"X-Aksha-User": self.owner}
        if content_type:
            out["Content-Type"] = content_type
        if self.token:
            out["Authorization"] = f"Bearer {self.token}"
        return out

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        *,
        content_type: str | None = "application/json",
        timeout: float | None = None,
    ) -> bytes:
        """One call; the raw response body, or :class:`ApiError` for an error status."""
        req = urllib.request.Request(
            self.url.rstrip("/") + API + path,
            data=body,
            method=method,
            headers=self.headers(content_type),
        )
        try:
            with self.opener(req, timeout=timeout or self.timeout) as response:
                data: bytes = response.read()
                return data
        except urllib.error.HTTPError as e:
            raw = e.read() or b"{}"
            try:
                err = json.loads(raw).get("error") or {}
            except (ValueError, AttributeError):
                err = {}
            raise ApiError(
                e.code,
                str(err.get("code", "http_error")),
                str(err.get("message", e.reason)),
                err.get("details"),
            ) from None

    def get(self, path: str) -> dict[str, Any]:
        """A JSON read."""
        loaded: dict[str, Any] = json.loads(self.request("GET", path) or b"{}")
        return loaded

    def optional(self, path: str) -> dict[str, Any] | None:
        """A JSON read the server may not have (404 or 405 is None)."""
        try:
            return self.get(path)
        except ApiError as e:
            if missing_route(e):
                return None
            raise

    def command(
        self, path: str, body: Mapping[str, Any], *, command_id: str | None = None
    ) -> CommandReceipt:
        """POST one command with a fresh command ID; a refusal is a refused receipt."""
        payload = {**body, "command_id": command_id or wire.new_command_id()}
        raw = json.dumps(payload).encode("utf-8")
        return self._send(lambda: self.request("POST", path, raw))

    def _send(self, call: Callable[[], bytes]) -> CommandReceipt:
        for attempt in range(RETRIES + 1):
            try:
                data = json.loads(call() or b"{}")
                return wire.receipt_from_json(data["receipt"])
            except ApiError as e:
                if isinstance(e.details, Mapping) and "command_id" in e.details:
                    return wire.receipt_from_json(e.details)
                raise
            except urllib.error.URLError:
                if attempt == RETRIES:
                    raise
        raise AssertionError("unreachable")

    # --- commands --------------------------------------------------------------------------

    def send_message(self, session: str, text: str, agent: str | None) -> CommandReceipt:
        """``POST /sessions/{session}/messages``."""
        body: dict[str, Any] = {"text": text}
        if agent:
            body["agent"] = agent
        return self.command(f"/sessions/{_q(session)}/messages", body)

    def steer(self, run: str, text: str, lane: str) -> CommandReceipt:
        """``POST /runs/{run}/steer``."""
        return self.command(f"/runs/{_q(run)}/steer", {"text": text, "lane": lane})

    def answer(
        self,
        interrupt: str,
        *,
        text: str | None = None,
        answers: Mapping[str, str] | None = None,
        agent: str | None = None,
    ) -> CommandReceipt:
        """``POST /interrupts/{interrupt}/answer``."""
        body: dict[str, Any] = {}
        if agent is not None:
            body["agent"] = agent
        elif answers is not None:
            body["answers"] = dict(answers)
        else:
            body["text"] = text or ""
        return self.command(f"/interrupts/{_q(interrupt)}/answer", body)

    def approve(self, approval: str, signature: str) -> CommandReceipt:
        """``POST /approvals/{approval}/approve`` with the full signature shown."""
        return self.command(f"/approvals/{_q(approval)}/approve", {"shown_signature": signature})

    def reject(self, approval: str, note: str | None) -> CommandReceipt:
        """``POST /approvals/{approval}/reject``."""
        return self.command(f"/approvals/{_q(approval)}/reject", {"note": note} if note else {})

    def stop(self, run: str, reason: str) -> CommandReceipt:
        """``POST /runs/{run}/stop``."""
        return self.command(f"/runs/{_q(run)}/stop", {"reason": reason})

    def upload(self, files: Mapping[str, bytes]) -> CommandReceipt:
        """``POST /inputs/upload`` (multipart), or ``POST /inputs`` on a server without it."""
        command_id = wire.new_command_id()
        body, boundary = multipart(files, {"command_id": command_id})
        ctype = f"multipart/form-data; boundary={boundary}"
        try:
            return self._send(
                lambda: self.request("POST", "/inputs/upload", body, content_type=ctype)
            )
        except ApiError as e:
            if not missing_route(e):
                raise
        return self.command("/inputs", inputs_body(files), command_id=command_id)

    # --- reads -----------------------------------------------------------------------------

    def capabilities(self) -> dict[str, Any]:
        """The capability record."""
        return self.get("/capabilities")

    def session(self, session: str) -> dict[str, Any]:
        """``GET /sessions/{session}``: its run and what it waits on."""
        return self.get(f"/sessions/{_q(session)}")

    def runs(self) -> list[dict[str, Any]]:
        """Every run's status, in start order."""
        runs: list[dict[str, Any]] = self.get("/runs")["runs"]
        return runs

    def sessions(self) -> list[dict[str, Any]]:
        """``GET /sessions``, or the sessions ``GET /runs`` names when the server lacks it."""
        data = self.optional("/sessions")
        if data is not None:
            rows: list[dict[str, Any]] = data.get("sessions") or []
            return rows
        return sessions_from_runs(self.runs())

    def artifacts(self, session: str) -> list[dict[str, Any]] | None:
        """``GET /artifacts?session=``, or None."""
        data = self.optional("/artifacts?" + urllib.parse.urlencode({"session": session}))
        return None if data is None else list(data.get("artifacts") or [])

    def artifact(self, ref: str) -> dict[str, Any] | None:
        """``GET /artifacts/{ref}``, or None."""
        return self.optional(f"/artifacts/{_q(ref)}")

    def documents(self) -> list[dict[str, Any]] | None:
        """``GET /documents``, or None."""
        data = self.optional("/documents")
        return None if data is None else list(data.get("documents") or [])

    def document(self, doc_key: str, version: int | None) -> str | None:
        """``GET /documents/{doc_key}``: its text (bytes decoded leniently), or None."""
        query = "" if version is None else f"?version={version}"
        try:
            raw = self.request("GET", f"/documents/{_q(doc_key)}{query}", content_type=None)
        except ApiError as e:
            if missing_route(e):
                return None
            raise
        return raw.decode("utf-8", errors="replace")

    # --- the stream ------------------------------------------------------------------------

    def events(self, session: str, cursor: int | None, stop: threading.Event) -> EventStream:
        """The session's SSE stream from ``cursor`` (module doc)."""

        def connect(after: int | None) -> Iterator[str]:
            path = f"/sessions/{_q(session)}/events"
            headers = self.headers(None)
            headers["Accept"] = "text/event-stream"
            if after is not None:
                path += f"?after={after}"
                headers["Last-Event-ID"] = str(after)
            req = urllib.request.Request(self.url.rstrip("/") + API + path, headers=headers)
            response = self.opener(req, timeout=STREAM_READ_TIMEOUT_S)
            self._live.append(response)
            try:
                for raw in response:
                    yield raw.decode("utf-8")
            finally:
                self._live.remove(response)
                response.close()

        return EventStream(connect, cursor=cursor, stop=stop)

    def close_streams(self) -> None:
        """Close any open stream connection, so a blocked read returns at once."""
        for response in list(self._live):
            with contextlib.suppress(OSError):
                response.close()


def missing_route(e: ApiError) -> bool:
    """The server has no such route (a 404 or 405 without Aksha's error body)."""
    return e.status in (404, 405) and e.code == "http_error"


def _q(part: str) -> str:
    return urllib.parse.quote(part, safe="")


def inputs_body(files: Mapping[str, bytes]) -> dict[str, Any]:
    """The ``POST /inputs`` body: UTF-8 files as text, the rest as base64."""
    text: dict[str, str] = {}
    binary: dict[str, str] = {}
    for path, data in files.items():
        try:
            text[path] = data.decode("utf-8")
        except UnicodeDecodeError:
            binary[path] = base64.b64encode(data).decode("ascii")
    body: dict[str, Any] = {"files": text}
    if binary:
        body["binary"] = binary
    return body


def multipart(files: Mapping[str, bytes], fields: Mapping[str, str]) -> tuple[bytes, str]:
    """A ``multipart/form-data`` body: one ``file`` part per file, named by its path."""
    boundary = "aksha-" + wire.new_command_id()[2:]
    out = bytearray()
    for name, value in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += value.encode("utf-8") + b"\r\n"
    for path, data in files.items():
        safe = path.replace('"', "%22")
        out += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{safe}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), boundary


def sessions_from_runs(runs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Session rows built from run statuses alone (a server without ``GET /sessions``)."""
    by_session: dict[str, dict[str, Any]] = {}
    for run in runs:
        session = run.get("session")
        if not session:
            continue
        row = by_session.setdefault(
            str(session), {"session": session, "title": session, "runs": 0, "archived": False}
        )
        row["runs"] += 1
        row["run"] = run.get("run")
        row["agent"] = run.get("agent")
        waiting = run.get("waiting_on") or []
        row["status"] = run_status_name(run)
        row["waiting"] = (
            {
                "kind": waiting[0].get("kind"),
                "id": waiting[0].get("approval_id") or waiting[0].get("interrupt_id"),
            }
            if waiting
            else None
        )
    return list(reversed(list(by_session.values())))


def run_status_name(run: Mapping[str, Any]) -> str:
    """The session status word of a run's status (``reads`` spec vocabulary)."""
    if run.get("waiting_on"):
        return "waiting"
    if run.get("state") == "running":
        return "running"
    outcome = run.get("outcome")
    if outcome == "complete":
        return "done"
    return str(outcome or "idle")


def read_inputs(paths: Sequence[str]) -> dict[str, bytes]:
    """The files under ``paths``: a file by its name, a folder's files by their path in it."""
    files: dict[str, bytes] = {}
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_file():
            files[path.name] = path.read_bytes()
        elif path.is_dir():
            for f in sorted(p for p in path.rglob("*") if p.is_file()):
                rel = f.relative_to(path)
                if not any(part.startswith(".") for part in rel.parts):
                    files[rel.as_posix()] = f.read_bytes()
        else:
            raise FileNotFoundError(f"no such file or folder: {raw}")
    return files
