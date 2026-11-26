"""``E2BSandbox``: a cloud backend of strength ``vm``, one remote E2B sandbox per session.

Decision 37: a session's execs all run in one remote sandbox, created on first use and
reused for every later exec of the session, so what one exec writes is there for the next.
The sandbox is kept by its ID in a small file per session under ``state_dir`` (the data
directory's ``sandbox/e2b/<owner>/<session>/e2b.json``); nothing secret is written there.

- **Selection.** Only ``AKSHA_SANDBOX=e2b`` chooses it; ``auto`` never sends a program to a
  cloud. It probes available when ``E2B_API_KEY`` is set and its URLs are well formed; the
  probe makes no request.
- **The key** is read from the environment on each request (``E2B_API_KEY``) and sent only
  as the control API's ``X-API-Key`` header. It is never logged, recorded, written to the
  state file or put in an error message.
- **Lifecycle.** Each exec connects to the session's sandbox, which resumes it if it was
  paused and restarts its idle timeout (``timeout_s``). When the session goes idle past
  that, E2B pauses the sandbox (``on_end="pause"``, so the next exec resumes it with its
  files) or kills it (``on_end="kill"``). ``clean`` kills it at once. A sandbox that is gone
  when the session comes back is replaced by a new, empty one, and the record says so
  (``replaced``). The intent to create is written to the state file before the create
  request and sent as sandbox metadata, so a crash between the two leaves a sandbox the next
  exec finds and adopts (``adopted``) and ``clean`` kills, never one Aksha cannot see. A
  sandbox for a request with no session is killed after its one box and never paused.
- **What a box gets.** The workspace is ``/home/user/work`` inside the remote sandbox; the
  inputs are uploaded over it and the program runs there with a fixed base environment,
  its argv passed as arguments (never assembled into a shell string), stdin from an
  uploaded file, and the CPU, memory, process, open-file and file-size limits applied with
  ``prlimit`` (the template must have it; E2B's base template does). The sandbox is created
  with internet access off. The program runs under ``setsid`` in its own process group: at
  the wall deadline, or on a stop, the whole group is signalled, and when the program exits
  whatever it left running in the group is killed before collection.
- **Outputs.** After the program, the files under the writable paths that it wrote or
  changed (every file, for ``collect="all"``) are listed in the sandbox, symlinks reported
  as denials and never followed, and downloaded up to ``max_collect_bytes``.

Limits of this backend, against bubblewrap: the whole remote workspace is writable, so a
policy's writable and protected paths decide only what is collected, not what the program
can write inside its own VM; the environment is E2B's process environment with the fixed
base over it, not a cleared one; an environment image (``/env``) is refused
(``environment_unsupported``: the template is the environment). Tested against a stubbed
HTTP server only, never the live service.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import struct
import threading
import urllib.error
import urllib.parse
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Literal

from aksha.adapters.sandbox.bwrap import SIGNAL_LIMITS, _denials
from aksha.adapters.sandbox.workspace import folder_name
from aksha.runtime.sandbox.errors import SANDBOX_UNAVAILABLE, SandboxRefused
from aksha.runtime.sandbox.ports import BlobReader, UnitHolder
from aksha.runtime.sandbox.types import (
    Availability,
    BoxRequest,
    BoxResult,
    Denial,
    ExecStatus,
    SessionRef,
    Strength,
    check_relative,
)

NAME = "e2b"
KEY_ENV = "E2B_API_KEY"
API_URL = "https://api.e2b.dev"
ENVD_URL = "https://49983-{sandbox}.e2b.app"
"""Where a sandbox's own daemon answers; ``{sandbox}`` is its ID."""
TEMPLATE = "base"
WORKDIR = "/home/user/work"
USER = "user"
STATE = "e2b.json"
REQUEST_TIMEOUT_S = 60.0
BASE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/user", "LANG": "C.UTF-8",
            "TMPDIR": "/tmp"}  # fmt: skip
BACKEND_FAILED = "backend_failed"
OnEnd = Literal["pause", "kill"]

_RUN = """# aksha-run
mkdir -p {workdir} && cd {workdir} || exit 125
in="$1"; mark="$2"; group="$3"; shift 3
while [ "$1" != "--" ]; do mkdir -p "$1"; shift; done; shift
touch "$mark"
setsid prlimit --cpu={cpu}:{cpu1} --as={mem} --nproc={nproc} --nofile={nofile} \\
  --fsize={fsize} --core=0 -- "$@" < "$in" &
pid=$!
echo "$pid" > "$group"
wait "$pid"; code=$?
kill -KILL "-$pid" 2>/dev/null
n=0; while kill -0 "-$pid" 2>/dev/null && [ "$n" -lt 40 ]; do sleep 0.05; n=$((n+1)); done
exit "$code"
"""
_SIGNAL = """# aksha-signal
group=$(cat "$1" 2>/dev/null) || exit 0
[ -n "$group" ] || exit 0
kill "-$2" "-$group" 2>/dev/null
[ "$2" = KILL ] || exit 0
n=0; while kill -0 "-$group" 2>/dev/null && [ "$n" -lt 40 ]; do sleep 0.05; n=$((n+1)); done
true
"""
_COLLECT = """# aksha-collect
cd {WORKDIR} || exit 125
mark="$1"; mode="$2"; shift 2
if [ "$mode" = changed ]; then
  find "$@" \\( -type f -o -type l \\) -newer "$mark" -printf '%y\\t%s\\t%p\\0' 2>/dev/null
else
  find "$@" \\( -type f -o -type l \\) -printf '%y\\t%s\\t%p\\0' 2>/dev/null
fi
rm -f "$mark" "/tmp/aksha-stdin-${mark##*-}" "/tmp/aksha-group-${mark##*-}"
true
"""


class E2BError(Exception):
    """A control or sandbox request failed; the message names the call, never a header."""


@dataclass(frozen=True)
class _Remote:
    sandbox: str
    token: str | None
    created: bool
    replaced: bool = False
    adopted: bool = False


class _Http:
    """Small JSON and byte requests over ``urllib``; headers never reach an error message."""

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    def call(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        ok: tuple[int, ...] = (200, 201, 204),
        missing: bool = False,
    ) -> bytes | None:
        """The response body; None for a 404 when ``missing``; ``E2BError`` otherwise."""
        import urllib.request  # here, so an offline process never loads an HTTP client

        req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        where = f"{method} {urllib.parse.urlsplit(url).path}"
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data: bytes = resp.read()
                if resp.status not in ok:
                    raise E2BError(f"{where}: HTTP {resp.status}")
                return data
        except urllib.error.HTTPError as exc:
            exc.close()
            if missing and exc.code == 404:
                return None
            raise E2BError(f"{where}: HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise E2BError(f"{where}: {type(exc).__name__}: {reason}") from None

    def stream(
        self, url: str, *, headers: Mapping[str, str], body: bytes, timeout_s: float
    ) -> IO[bytes]:
        """An open streaming response, for the caller to read and close."""
        import urllib.request

        req = urllib.request.Request(url, data=body, method="POST", headers=dict(headers))
        where = f"POST {urllib.parse.urlsplit(url).path}"
        try:
            resp: IO[bytes] = urllib.request.urlopen(req, timeout=timeout_s)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise E2BError(f"{where}: HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise E2BError(f"{where}: {type(exc).__name__}: {reason}") from None
        return resp


def envelope(message: Mapping[str, Any], flags: int = 0) -> bytes:
    """One Connect streaming envelope: a flag byte, a big-endian length, the JSON."""
    data = json.dumps(message).encode()
    return struct.pack(">BI", flags, len(data)) + data


def read_envelope(stream: IO[bytes]) -> tuple[int, dict[str, Any]] | None:
    """The next envelope's flags and JSON message, or None at the end of the stream."""
    head = _read_exactly(stream, 5)
    if head is None:
        return None
    flags, size = struct.unpack(">BI", head)
    data = _read_exactly(stream, size) if size else b""
    if data is None:
        return None
    return flags, (json.loads(data) if data else {})


def _read_exactly(stream: IO[bytes], n: int) -> bytes | None:
    out = bytearray()
    while len(out) < n:
        chunk = stream.read(n - len(out))
        if not chunk:
            return None
        out.extend(chunk)
    return bytes(out)


class _RemoteBox:
    """A running remote process as a ``KillableUnit``: its whole process group is signalled.

    The run script starts the program with ``setsid``, so the program and every child it
    starts are one process group whose ID the script writes to ``group``. A stop or the
    wall deadline signals that group (a second, short process in the sandbox), then the
    script's own process; when the program exits, the script kills what is left of the
    group before collection, as the end of a bubblewrap box does.
    """

    def __init__(self, backend: E2BSandbox, remote: _Remote, group: str | None = None) -> None:
        self._backend, self._remote, self._group = backend, remote, group
        self.pid: int | None = None
        self.done = threading.Event()
        self.killed = False

    def _signal(self, name: str) -> None:
        if self.done.is_set():
            return
        if self._group is not None:
            with contextlib.suppress(E2BError):
                self._backend._signal_group(self._remote, self._group, name)
        if self.pid is not None:
            with contextlib.suppress(E2BError):
                self._backend._signal(self._remote, self.pid, "SIGNAL_SIG" + name)

    def request_settle(self) -> None:
        self._signal("TERM")

    def terminate(self) -> None:
        self.killed = True
        self._signal("KILL")

    def settled(self) -> bool:
        return self.done.is_set()

    def wait(self, seconds: float) -> bool:
        return self.done.wait(seconds)


class E2BSandbox:
    """The E2B backend (module doc)."""

    name = NAME
    strength: Strength = "vm"

    def __init__(
        self,
        *,
        state_dir: Path,
        env: Mapping[str, str] | None = None,
        api_url: str = API_URL,
        envd_url: str = ENVD_URL,
        template: str = TEMPLATE,
        timeout_s: int = 900,
        on_end: OnEnd = "pause",
    ) -> None:
        """Keep each session's sandbox ID under ``state_dir``; read the key from ``env``."""
        self._state_dir = state_dir
        self._env = os.environ if env is None else env
        self._api = api_url.rstrip("/")
        self._envd = envd_url
        self._template = template
        self._timeout_s = timeout_s
        self._on_end = on_end
        self._http = _Http(REQUEST_TIMEOUT_S)
        self._locks: dict[SessionRef, threading.Lock] = {}
        self._guard = threading.Lock()

    def __repr__(self) -> str:
        """Names the backend and its API, never the key."""
        return f"E2BSandbox(api_url={self._api!r}, template={self._template!r})"

    # --- probe ------------------------------------------------------------------------

    def probe(self) -> Availability:
        """Available when the key is set and the URLs are well formed; makes no request."""

        def no(reason: str) -> Availability:
            return Availability(NAME, self.strength, False, reason)

        if not self._env.get(KEY_ENV):
            return no(f"{KEY_ENV} is not set")
        for url in (self._api, self._envd.replace("{sandbox}", "x")):
            parts = urllib.parse.urlsplit(url)
            if parts.scheme not in ("https", "http") or not parts.netloc:
                return no(f"{url!r} is not an http(s) URL")
        if self._on_end not in ("pause", "kill"):
            return no(f"on_end is {self._on_end!r}; use pause or kill")
        return Availability(NAME, self.strength, True)

    def version(self) -> str:
        """The control API and template this backend uses."""
        return f"e2b-api {self._api} template {self._template}"

    # --- sessions ---------------------------------------------------------------------

    def _lock(self, ref: SessionRef) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(ref, threading.Lock())

    def _state(self, ref: SessionRef) -> Path:
        return self._state_dir / folder_name(ref.owner) / folder_name(ref.session) / STATE

    def _known(self, ref: SessionRef) -> dict[str, Any] | None:
        try:
            return dict(json.loads(self._state(ref).read_text()))
        except (OSError, ValueError):
            return None

    def sessions(self) -> tuple[SessionRef, ...]:
        """Every session with a remote sandbox kept by ID."""
        out: list[SessionRef] = []
        if not self._state_dir.is_dir():
            return ()
        for path in sorted(self._state_dir.glob(f"*/*/{STATE}")):
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
                known = json.loads(path.read_text())
                out.append(SessionRef(str(known["owner"]), str(known["session"])))
        return tuple(out)

    def clean(self, ref: SessionRef) -> Mapping[str, Any] | None:
        """Kill the session's remote sandbox (and any a crash left unrecorded); forget it."""
        known = self._known(ref)
        if known is None:
            return None
        with self._lock(ref):
            killed: list[str] = []
            if known.get("sandbox"):
                killed.append(str(known["sandbox"]))
            elif known.get("intent"):
                killed.extend(self._intended(str(known["intent"])))
            for sandbox in killed:
                self._kill(sandbox)
            state = self._state(ref)
            with contextlib.suppress(OSError):
                state.unlink()
                state.parent.rmdir()
        out: dict[str, Any] = {
            "owner": ref.owner,
            "session": ref.session,
            "sandbox": killed[0] if killed else None,
            "killed": bool(killed),
        }
        if len(killed) > 1:
            out["also_killed"] = killed[1:]
        return out

    # --- one box ----------------------------------------------------------------------

    def run(self, request: BoxRequest, blobs: BlobReader, unit: UnitHolder) -> BoxResult:
        """Run one box in the session's remote sandbox, or in a new one killed afterwards."""
        probe = self.probe()
        if not probe.available:
            raise SandboxRefused(SANDBOX_UNAVAILABLE, f"e2b: {probe.reason}")
        if request.policy.network != "none":
            raise SandboxRefused("network_unsupported", "e2b boxes run with internet access off")
        if request.environment is not None:
            why = "e2b: the template is the environment; /env is not mounted"
            raise SandboxRefused("environment_unsupported", why)
        ref = request.session
        try:
            if ref is None:
                remote = self._create(None)
                try:
                    return self._box(request, remote, blobs, unit, fresh=True)
                finally:
                    with contextlib.suppress(E2BError):
                        self._kill(remote.sandbox)
            with self._lock(ref):
                remote = self._connect_or_create(ref)
                return self._box(request, remote, blobs, unit, fresh=False)
        except E2BError as exc:
            raise SandboxRefused(BACKEND_FAILED, f"e2b: {exc}") from None

    def _box(
        self,
        request: BoxRequest,
        remote: _Remote,
        blobs: BlobReader,
        unit: UnitHolder,
        *,
        fresh: bool,
    ) -> BoxResult:
        policy, limits = request.policy, request.policy.limits
        for rel, ref in request.tree.entries.items() if request.tree is not None else ():
            self._upload(remote, f"{WORKDIR}/{rel}", blobs.get(ref))
        for rel, data in request.files.items():
            self._upload(remote, f"{WORKDIR}/{rel}", data)
        tag = uuid.uuid4().hex[:12]  # names this exec's temporary files in the remote /tmp
        stdin, mark = f"/tmp/aksha-stdin-{tag}", f"/tmp/aksha-mark-{tag}"
        group = f"/tmp/aksha-group-{tag}"
        self._upload(remote, stdin, request.stdin)
        writable = [w for w in policy.writable if w != "."]
        script = _RUN.format(workdir=WORKDIR, cpu=limits.cpu_s, cpu1=limits.cpu_s + 1,
                             mem=limits.memory_mb * 1_048_576, nproc=limits.max_processes,
                             nofile=limits.max_open_files, fsize=limits.max_file_bytes)  # fmt: skip
        args = ["-c", script, "aksha", stdin, mark, group, *writable, "--", *request.argv]
        box = _RemoteBox(self, remote, group)
        unit.attach(box)
        try:
            code, out, err, timed_out, out_cut, err_cut = self._process(
                remote, args, limits.wall_ms, limits.max_output_bytes, box
            )
        finally:
            box.done.set()
            unit.detach()
        denials = list(_denials(err))
        collected, rejected = self._collect(remote, request, mark, denials)
        status: ExecStatus
        if timed_out or (box.killed and code is None):
            status, code = "exec_timeout", None
        elif rejected:
            status = "output_rejected"
        elif code is not None and (code in SIGNAL_LIMITS or (
                any(d.kind == "limit" for d in denials) and code != 0)):  # fmt: skip
            status = "exec_limit"
        elif code == 0:
            status = "ok"
        else:
            status = "exec_failed"
        workspace = None
        if not fresh:
            workspace = {"backend": NAME, "sandbox": remote.sandbox, "folder": WORKDIR,
                         "created": remote.created, "template": self._template,
                         "on_end": self._on_end}  # fmt: skip
            if remote.replaced:
                workspace["replaced"] = True
            if remote.adopted:
                workspace["adopted"] = True
        return BoxResult(status, code, out, err, tuple(denials), {} if rejected else collected,
                         out_cut, err_cut, workspace)  # fmt: skip

    # --- the remote calls ---------------------------------------------------------------

    def _control(self) -> dict[str, str]:
        return {"X-API-Key": self._env.get(KEY_ENV, ""), "Content-Type": "application/json"}

    def _daemon(self, remote: _Remote, content: str | None = None) -> dict[str, str]:
        out = {"Authorization": "Basic " + base64.b64encode(f"{USER}:".encode()).decode()}
        if remote.token:
            out["X-Access-Token"] = remote.token
        if content:
            out["Content-Type"] = content
        return out

    def _envd_url(self, remote: _Remote, path: str) -> str:
        return self._envd.replace("{sandbox}", urllib.parse.quote(remote.sandbox, safe="")) + path

    def _create(self, ref: SessionRef | None, intent: str | None = None) -> _Remote:
        metadata = {} if ref is None else {"aksha_owner": ref.owner, "aksha_session": ref.session}
        if intent is not None:
            metadata["aksha_intent"] = intent
        # A sandbox for no session is killed after its one box; if that kill is lost, its
        # timeout kills it rather than pausing it.
        pause = self._on_end == "pause" and ref is not None
        body = {"templateID": self._template, "timeout": self._timeout_s, "autoPause": pause,
                "secure": True, "allow_internet_access": False,
                "metadata": metadata}  # fmt: skip
        raw = self._http.call("POST", f"{self._api}/sandboxes", headers=self._control(),
                              body=json.dumps(body).encode())  # fmt: skip
        got = json.loads(raw or b"{}")
        sandbox = got.get("sandboxID")
        if not isinstance(sandbox, str) or not sandbox:
            raise E2BError("POST /sandboxes: no sandboxID in the answer")
        return _Remote(sandbox, got.get("envdAccessToken"), created=True)

    def _write_state(self, ref: SessionRef, **fields: str) -> None:
        state = self._state(ref)
        state.parent.mkdir(parents=True, exist_ok=True)
        tmp = state.with_suffix(".tmp")
        tmp.write_text(json.dumps({"owner": ref.owner, "session": ref.session, **fields},
                                  sort_keys=True))  # fmt: skip
        tmp.replace(state)

    def _connect(self, sandbox: str) -> _Remote | None:
        raw = self._http.call(
            "POST", f"{self._api}/sandboxes/{urllib.parse.quote(sandbox, safe='')}/connect",
            headers=self._control(), body=json.dumps({"timeout": self._timeout_s}).encode(),
            missing=True,
        )  # fmt: skip
        if raw is None:
            return None
        return _Remote(sandbox, json.loads(raw or b"{}").get("envdAccessToken"), created=False)

    def _intended(self, intent: str) -> list[str]:
        """The sandboxes created under ``intent``: a create whose answer a crash lost."""
        query = urllib.parse.urlencode([("metadata", f"aksha_intent={intent}"),
                                        ("state", "running"), ("state", "paused")])  # fmt: skip
        raw = self._http.call("GET", f"{self._api}/v2/sandboxes?{query}",
                              headers=self._control())  # fmt: skip
        listed = json.loads(raw or b"[]")
        if not isinstance(listed, list):
            raise E2BError("GET /v2/sandboxes: the answer is not a list")
        return [str(item["sandboxID"]) for item in listed
                if isinstance(item, dict) and item.get("sandboxID")]  # fmt: skip

    def _connect_or_create(self, ref: SessionRef) -> _Remote:
        """The session's sandbox: the one kept by ID, one a crash left unrecorded, or a new one.

        The intent to create is written before the create request and rides on the sandbox
        as metadata, so a crash between the two leaves a sandbox this finds and adopts,
        never one that list and clean cannot see.
        """
        known = self._known(ref) or {}
        replaced = False
        if known.get("sandbox"):
            remote = self._connect(str(known["sandbox"]))
            if remote is not None:
                return remote
            replaced = True  # the sandbox timed out and was killed, or was removed elsewhere
        elif known.get("intent"):
            found = self._intended(str(known["intent"]))
            for extra in found[1:]:
                self._kill(extra)
            remote = self._connect(found[0]) if found else None
            if remote is not None:
                self._write_state(ref, sandbox=remote.sandbox)
                return _Remote(remote.sandbox, remote.token, created=False, adopted=True)
        intent = uuid.uuid4().hex
        self._write_state(ref, intent=intent)
        remote = self._create(ref, intent)
        self._write_state(ref, sandbox=remote.sandbox)
        return _Remote(remote.sandbox, remote.token, created=True, replaced=replaced)

    def _kill(self, sandbox: str) -> None:
        self._http.call("DELETE", f"{self._api}/sandboxes/{urllib.parse.quote(sandbox, safe='')}",
                        headers=self._control(), missing=True)  # fmt: skip

    def _upload(self, remote: _Remote, path: str, data: bytes) -> None:
        boundary = "aksha-" + uuid.uuid4().hex
        name = path.rsplit("/", 1)[-1].replace('"', "_")
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                ).encode() + data + f"\r\n--{boundary}--\r\n".encode()  # fmt: skip
        query = urllib.parse.urlencode({"path": path, "username": USER})
        content = f"multipart/form-data; boundary={boundary}"
        self._http.call("POST", self._envd_url(remote, f"/files?{query}"), body=body,
                        headers=self._daemon(remote, content))  # fmt: skip

    def _download(self, remote: _Remote, path: str) -> bytes:
        query = urllib.parse.urlencode({"path": path, "username": USER})
        return self._http.call("GET", self._envd_url(remote, f"/files?{query}"),
                               headers=self._daemon(remote)) or b""  # fmt: skip

    def _signal(self, remote: _Remote, pid: int, signal: str) -> None:
        body = json.dumps({"process": {"pid": pid}, "signal": signal}).encode()
        self._http.call("POST", self._envd_url(remote, "/process.Process/SendSignal"), body=body,
                        headers=self._daemon(remote, "application/json"))  # fmt: skip

    def _signal_group(self, remote: _Remote, group: str, name: str) -> None:
        """Send ``name`` (``TERM`` or ``KILL``) to the process group recorded in ``group``."""
        helper = _RemoteBox(self, remote)
        try:
            self._process(remote, ["-c", _SIGNAL, "aksha", group, name], 10_000, 4096, helper)
        finally:
            helper.done.set()

    def _process(
        self, remote: _Remote, args: list[str], wall_ms: int, cap: int, box: _RemoteBox
    ) -> tuple[int | None, bytes, bytes, bool, bool, bool]:
        """Start ``/bin/sh args`` in the workspace; exit code, output, and what was cut."""
        start = {"process": {"cmd": "/bin/sh", "args": args, "envs": BASE_ENV, "cwd": "/"}}
        headers = {**self._daemon(remote, "application/connect+json"),
                   "Connect-Protocol-Version": "1",
                   "Connect-Timeout-Ms": str(wall_ms + 5_000)}  # fmt: skip
        stream = self._http.stream(self._envd_url(remote, "/process.Process/Start"),
                                   headers=headers, body=envelope(start),
                                   timeout_s=wall_ms / 1000 + 10)  # fmt: skip
        out, err = bytearray(), bytearray()
        cut = {"out": False, "err": False}
        ended: dict[str, Any] = {}
        failure: list[str] = []

        def keep(buf: bytearray, key: str, data: bytes) -> None:
            room = cap - len(buf)
            if room > 0:
                buf.extend(data[:room])
            if len(data) > max(room, 0):
                cut[key] = True

        def read() -> None:
            with contextlib.suppress(OSError, ValueError):
                while (got := read_envelope(stream)) is not None:
                    flags, message = got
                    if flags & 0x02:
                        if message.get("error"):
                            failure.append(str(message["error"].get("code", "error")))
                        return
                    event = message.get("event", {})
                    if "start" in event:
                        box.pid = int(event["start"].get("pid", 0)) or None
                    data = event.get("data", {})
                    if "stdout" in data:
                        keep(out, "out", base64.b64decode(data["stdout"]))
                    if "stderr" in data:
                        keep(err, "err", base64.b64decode(data["stderr"]))
                    if "end" in event:
                        ended.update(event["end"])

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(wall_ms / 1000)
        timed_out = reader.is_alive()
        if timed_out:
            box.terminate()
            reader.join(5)
        with contextlib.suppress(OSError):
            stream.close()
        if not ended and not timed_out:
            why = failure[0] if failure else "the process stream ended without an exit"
            if not box.killed:
                raise E2BError(f"process: {why}")
        code: int | None = None
        if ended:
            status = str(ended.get("status", ""))
            code = int(ended.get("exitCode", 0)) if not status.startswith("signal") else -1
        return code, bytes(out), bytes(err), timed_out, cut["out"], cut["err"]

    def _collect(
        self, remote: _Remote, request: BoxRequest, mark: str, denials: list[Denial]
    ) -> tuple[dict[str, bytes], bool]:
        """The writable paths' files (changed ones, or all), symlink-safe and capped."""
        policy = request.policy
        roots = ["."] if "." in policy.writable else [f"./{w}" for w in policy.writable]
        mode = "changed" if request.session is not None and request.collect == "changed" else "all"
        listing = _RemoteBox(self, remote)
        code, out, _err, _t, _c1, _c2 = self._process(
            remote, ["-c", _COLLECT.replace("{WORKDIR}", WORKDIR), "aksha", mark, mode, *roots],
            30_000, 16 * 1_048_576,
            listing,
        )  # fmt: skip
        listing.done.set()
        if code != 0:
            raise E2BError("listing the written files failed")
        cap = policy.limits.max_collect_bytes
        protected = policy.protected
        collected: dict[str, bytes] = {}
        total = 0
        for line in out.decode("utf-8", "replace").split("\0"):
            kind, _, rest = line.partition("\t")
            size, _, rel = rest.partition("\t")
            rel = rel.removeprefix("./")
            if not rel or any(rel == p or rel.startswith(p + "/") for p in protected):
                continue
            try:
                check_relative(rel)  # the listing comes from the box: never trust a path
            except ValueError:
                denials.append(Denial("output", "skipped a path that is not clean"))
                continue
            if kind == "l":
                denials.append(Denial("symlink", f"skipped {rel}"))
                continue
            if total + (int(size) if size.isdigit() else 0) > cap:
                denials.append(Denial("output", f"collected output over {cap} bytes"))
                return {}, True
            data = self._download(remote, f"{WORKDIR}/{rel}")
            total += len(data)  # what came back, not what the listing claimed
            if total > cap:
                denials.append(Denial("output", f"collected output over {cap} bytes"))
                return {}, True
            collected[rel] = data
        return collected, False
