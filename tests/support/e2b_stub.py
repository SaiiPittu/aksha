"""A stubbed E2B service on a local port: the control API and each sandbox's daemon.

Synthetic, for the E2B adapter's tests only: it runs no program. A test sets
``program(sandbox, argv, stdin) -> Ran`` to say what a "run" printed and wrote; the stub
keeps each sandbox's files in memory (under the adapter's workspace path), with a logical
clock so ``-newer`` listings work, and answers the adapter's listing script from them. It
records every request with its headers, so a test can check where the key went.
"""

from __future__ import annotations

import base64
import json
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

WORKDIR = "/home/user/work"


@dataclass
class Ran:
    """What one stubbed program did."""

    stdout: bytes = b""
    stderr: bytes = b""
    code: int = 0
    writes: dict[str, bytes] = field(default_factory=dict)
    links: tuple[str, ...] = ()
    hang_s: float = 0.0


@dataclass
class Box:
    """One stubbed sandbox."""

    ident: str
    body: dict[str, Any]
    files: dict[str, tuple[bytes, int, str]] = field(default_factory=dict)
    """Absolute path to (bytes, logical time written, ``f`` or ``l``)."""
    marks: dict[str, int] = field(default_factory=dict)
    alive: bool = True
    killed: threading.Event = field(default_factory=threading.Event)
    connects: int = 0


def _envelope(message: dict[str, Any], flags: int = 0) -> bytes:
    data = json.dumps(message).encode()
    return struct.pack(">BI", flags, len(data)) + data


class E2BStub:
    """The stub service (module doc); use as a context manager."""

    def __init__(self, key: str = "SYN-e2b-key-0000") -> None:
        self.key = key
        self.boxes: dict[str, Box] = {}
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        """Each request's method, path and headers (names lower-cased)."""
        self.signals: list[tuple[str, int, str]] = []
        self.group_signals: list[tuple[str, str, str]] = []
        """Each process-group signal the adapter's helper sent: sandbox, group file, signal."""
        self.clock = 0
        self.fail_create = False
        self.lose_create_answer = False
        """Create the sandbox, then answer 500: a crash between the create and its answer."""
        self.program: Callable[[Box, list[str], bytes], Ran] = lambda b, a, s: Ran()
        self.listing: Callable[[list[str]], list[str]] = lambda lines: lines
        """Rewrites the collect listing, to play a box that lies about its files."""
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _body(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _answer(self, code: int, body: Any = None) -> None:
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self) -> None:
                stub.requests.append((self.command, self.path,
                                     {k.lower(): v for k, v in self.headers.items()}))  # fmt: skip
                stub.route(self)

            do_GET = do_POST = do_DELETE = _handle

        self.Handler = Handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True)

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def envd_url(self) -> str:
        return self.api_url + "/envd/{sandbox}"

    def __enter__(self) -> E2BStub:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for box in self.boxes.values():
            box.killed.set()
        self.server.shutdown()
        self.server.server_close()

    def tick(self) -> int:
        self.clock += 1
        return self.clock

    # --- routing --------------------------------------------------------------------------

    def route(self, h: Any) -> None:
        parts = urlsplit(h.path)
        segs = [s for s in parts.path.split("/") if s]
        if segs[:1] == ["sandboxes"] or segs[:2] == ["v2", "sandboxes"]:
            if h.headers.get("X-API-Key") != self.key:
                h._answer(401, {"message": "bad key"})
                return
            if segs[0] == "v2":
                return self._listed(h, parse_qs(parts.query))
            return self._control(h, segs[1:])
        if segs[:1] == ["envd"] and len(segs) >= 3:
            box = self.boxes.get(segs[1])
            if box is None or not box.alive:
                h._answer(502)
                return
            if "X-API-Key" in h.headers:
                h._answer(400, {"message": "control key sent to a sandbox"})
                return
            return self._daemon(h, box, "/".join(segs[2:]), parse_qs(parts.query))
        h._answer(404)
        return

    def _control(self, h: Any, rest: list[str]) -> None:
        body = h._body()
        if h.command == "POST" and not rest:
            if self.fail_create:
                h._answer(500, {"message": "SYN failure"})
                return
            ident = f"SYN-sbx-{len(self.boxes) + 1}"
            self.boxes[ident] = Box(ident, json.loads(body or b"{}"))
            if self.lose_create_answer:
                self.lose_create_answer = False
                h._answer(500, {"message": "SYN lost answer"})
                return
            h._answer(201, {"sandboxID": ident, "envdAccessToken": f"tok-{ident}"})
            return
        box = self.boxes.get(rest[0]) if rest else None
        if box is None or not box.alive:
            h._answer(404, {"message": "not found"})
            return
        if h.command == "POST" and rest[1:] == ["connect"]:
            box.connects += 1
            h._answer(200, {"sandboxID": box.ident, "envdAccessToken": f"tok-{box.ident}"})
            return
        if h.command == "DELETE" and len(rest) == 1:
            box.alive = False
            box.killed.set()
            h._answer(204)
            return
        h._answer(404)
        return

    def _listed(self, h: Any, query: dict[str, list[str]]) -> None:
        wanted = dict(pair.split("=", 1) for pair in query.get("metadata", []))
        h._answer(200, [{"sandboxID": box.ident} for box in self.boxes.values()
                        if box.alive and all(box.body.get("metadata", {}).get(k) == v
                                             for k, v in wanted.items())])  # fmt: skip

    def _daemon(self, h: Any, box: Box, what: str, query: dict[str, list[str]]) -> None:
        if h.headers.get("X-Access-Token") != f"tok-{box.ident}":
            h._answer(401)
            return
        if what == "files":
            path = query["path"][0]
            if h.command == "POST":
                box.files[path] = (_multipart(h._body(), h.headers["Content-Type"]),
                                   self.tick(), "f")  # fmt: skip
                h._answer(200, [{"path": path}])
                return
            got = box.files.get(path)
            if got is None:
                h._answer(404)
                return
            h.send_response(200)
            h.send_header("Content-Length", str(len(got[0])))
            h.end_headers()
            h.wfile.write(got[0])
            return None
        if what == "process.Process/SendSignal":
            body = json.loads(h._body())
            self.signals.append((box.ident, int(body["process"]["pid"]), body["signal"]))
            box.killed.set()
            h._answer(200, {})
            return
        if what == "process.Process/Start":
            raw = h._body()
            start = json.loads(raw[5:])
            return self._start(h, box, start["process"])
        h._answer(404)
        return

    def _start(self, h: Any, box: Box, process: dict[str, Any]) -> None:
        args: list[str] = process["args"]
        script = args[1]
        h.send_response(200)
        h.send_header("Content-Type", "application/connect+json")
        h.end_headers()

        def send(event: dict[str, Any]) -> None:
            h.wfile.write(_envelope({"event": event}))
            h.wfile.flush()

        send({"start": {"pid": 4242}})
        if script.startswith("# aksha-run"):
            stdin_path, mark, group = args[3], args[4], args[5]
            box.files[group] = (b"4242\n", self.tick(), "f")
            argv = args[args.index("--") + 1 :]
            box.marks[mark] = self.tick()
            ran = self.program(box, argv, box.files.get(stdin_path, (b"", 0, "f"))[0])
            for rel, data in ran.writes.items():
                box.files[f"{WORKDIR}/{rel}"] = (data, self.tick(), "f")
            for rel in ran.links:
                box.files[f"{WORKDIR}/{rel}"] = (b"", self.tick(), "l")
            if ran.stdout:
                send({"data": {"stdout": base64.b64encode(ran.stdout).decode()}})
            if ran.stderr:
                send({"data": {"stderr": base64.b64encode(ran.stderr).decode()}})
            if ran.hang_s and box.killed.wait(ran.hang_s):
                box.killed.clear()
                send({"end": {"exited": True, "status": "signal: killed"}})
            else:
                end: dict[str, Any] = {"exited": True, "status": f"exit status {ran.code}"}
                if ran.code:
                    end["exitCode"] = ran.code
                send({"end": end})
        elif script.startswith("# aksha-signal"):
            self.group_signals.append((box.ident, args[3], args[4]))
            box.killed.set()
            send({"end": {"exited": True, "status": "exit status 0"}})
        elif script.startswith("# aksha-collect"):
            mark, mode, roots = args[3], args[4], args[5:]
            since = box.marks.get(mark, 0)
            lines = []
            for path, (data, at, kind) in sorted(box.files.items()):
                if not path.startswith(WORKDIR + "/"):
                    continue
                rel = path[len(WORKDIR) + 1 :]
                for root in roots:
                    bare = root.removeprefix("./")
                    if root == "." or rel == bare or rel.startswith(bare + "/"):
                        if mode == "all" or at > since:
                            lines.append(f"{kind}\t{len(data)}\t./{rel}\0")
                        break
            lines = self.listing(lines)
            send({"data": {"stdout": base64.b64encode("".join(lines).encode()).decode()}})
            send({"end": {"exited": True, "status": "exit status 0"}})
        h.wfile.write(_envelope({}, flags=0x02))
        h.wfile.flush()


def _multipart(body: bytes, content_type: str) -> bytes:
    boundary = content_type.split("boundary=", 1)[1].encode()
    part = body.split(b"--" + boundary)[1]
    data = part.split(b"\r\n\r\n", 1)[1]
    return data.removesuffix(b"\r\n")
