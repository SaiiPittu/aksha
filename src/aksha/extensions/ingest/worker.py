"""Parsers in a worker process: a wall-clock, memory and CPU cap, no network, a clean gap.

Every parser :mod:`aksha.extensions.ingest.parse` calls (the sniffer's ZIP reader, pypdf,
python-docx, python-pptx, openpyxl, Pillow, the email parser) reads untrusted bytes. A
:class:`ParserPool` runs that parsing in a separate Python process, so a file that makes a
parser spin, allocate without bound or crash stops that process and never the runtime:

1. **Limits.** The worker sets its own limits before it reads any input: its address space
   (``RLIMIT_AS``) to ``Caps.parser_memory_bytes``; per job its CPU time (``RLIMIT_CPU``) to
   what it has used plus ``Caps.parser_cpu_s``; no core files, no file writes
   (``RLIMIT_FSIZE`` 0) and no new processes (``RLIMIT_NPROC`` 0). The host holds the wall
   clock: no result within ``Caps.parser_time_ms`` and the worker's whole process group is
   killed (``SIGKILL``; the worker starts its own session).
2. **No network.** The worker enters a new user and network namespace (``unshare``), which
   has only a loopback that is down, before it reads input. Where the kernel refuses
   unprivileged namespaces, it runs without one and says so in :attr:`ParserPool.isolation`.
3. **A clean failure.** A worker that misses the deadline or goes over the CPU cap leaves a
   gap ``unreadable`` whose detail starts ``parser_timeout``; one that runs out of memory,
   ``parser_memory``; one that dies any other way or sends a reply the protocol does not
   allow, ``parser_crashed``. The worker reports the type check before it converts, so the
   document keeps its detected type when the converter is what failed.
4. **A small pool.** Idle workers are kept (``size``) and reused for jobs with the same
   limits, each for at most ``max_jobs`` jobs, so the interpreter and the parser libraries
   load once. A worker that hit a limit or died is discarded, never reused. At most
   ``max_workers`` parse at once; a caller past that waits for a slot.
5. **Inside bubblewrap when configured.** With ``jail`` (the composition root passes the
   bubblewrap backend's, ADR-P12, when that sandbox is configured and probes available)
   every job runs in its own box with no network, a read-only system and Python, and
   read-only access to the one input file, bound at :data:`BOX_INPUT`. A jailed worker is
   never pooled: the box holds one input.

The reply is JSON with raw byte blobs (:mod:`aksha.extensions.ingest.wire`), checked field
by field, never unpickled: a worker that a hostile file took over can at worst make its
own document's gap or text wrong, and only within the reply cap.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import math
import os
import resource
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from aksha.extensions.ingest import wire
from aksha.extensions.ingest.parse import (
    PARSER_CRASHED,
    PARSER_MEMORY,
    PARSER_TIMEOUT,
    Parsed,
    failed,
)
from aksha.extensions.ingest.sniff import Sniffed
from aksha.extensions.ingest.types import Caps, Gap

DEFAULT_TARGET = "aksha.extensions.ingest.parse:parse_in_process"
"""The function a worker runs for each job, as ``module:function``."""
BOX_INPUT = "/input/document"
"""Where a jailed worker finds its one input file."""
STARTUP_S = 30.0
"""How long a new worker may take to load the interpreter and the parser libraries."""
MAX_REPLY_BYTES = 256 * 1024 * 1024
"""A reply's cap beyond the decoded pixels it may carry (four bytes per capped pixel)."""
WORKER_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
"""A worker's whole environment: no key or token of the host reaches it."""
JAILED = "bubblewrap: every namespace unshared, so no network; the input file read-only"
PRELOAD = ("pypdf", "docx", "pptx", "openpyxl", "PIL.Image", "PIL.ImageCms", "PIL.ImageOps")

_BOOT = (
    "import json, sys; sys.path[:] = json.loads(sys.argv[1]); "
    "from aksha.extensions.ingest.worker import main; main(json.loads(sys.argv[2]))"
)

Jail = Callable[[Sequence[str], Sequence[str], str, str], list[str]]
"""``(argv, read-only paths, host input file, box input path) -> argv`` of a jailed worker."""


def _gap(detail: str) -> Gap:
    return Gap("unreadable", detail, ("parse_fast",))


# --- the worker process -----------------------------------------------------------------


def _isolate() -> list[str]:
    """Enter a new user and network namespace; what isolation the worker got."""
    flags = getattr(os, "CLONE_NEWUSER", 0) | getattr(os, "CLONE_NEWNET", 0)
    unshare = getattr(os, "unshare", None)
    if unshare is None or not flags:
        return ["network: not isolated (no unshare on this platform)"]
    try:
        unshare(flags)
    except OSError as e:
        return [f"network: not isolated (unshare refused: {e.strerror or e})"]
    return ["network: own namespace, no interfaces up"]


def _limit(which: int, soft: int, hard: int) -> None:
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(which, (soft, hard))


def _cpu_used() -> float:
    use = resource.getrusage(resource.RUSAGE_SELF)
    return use.ru_utime + use.ru_stime


def _read_exact(stream: IO[bytes], n: int) -> bytes | None:
    data = stream.read(n)
    if len(data) < n:
        return None
    return data


def _read_job(stream: IO[bytes]) -> tuple[dict[str, Any], list[bytes]] | None:
    head = _read_exact(stream, wire.HEADER_BYTES)
    if head is None:
        return None
    body = _read_exact(stream, wire.header_length(head))
    if body is None:
        return None
    obj, sizes = wire.load(body)
    blobs = []
    for n in sizes:
        got = _read_exact(stream, n)
        if got is None:
            return None
        blobs.append(got)
    return obj, blobs


def _target(spec: str) -> Callable[..., Parsed]:
    module, _, name = spec.partition(":")
    fn: Callable[..., Parsed] = getattr(importlib.import_module(module), name)
    return fn


def main(config: dict[str, Any]) -> None:
    """The worker's loop: limits, then one job per frame until stdin closes (module doc)."""
    out = sys.stdout.buffer
    inp = sys.stdin.buffer
    sys.stdout = sys.stderr  # a stray print never corrupts the protocol
    isolation = _isolate()
    memory, cpu, jobs = int(config["memory_bytes"]), int(config["cpu_s"]), int(config["max_jobs"])
    if jobs > 1:  # a pooled worker loads the parser libraries once, before its memory cap
        for module in PRELOAD:
            with contextlib.suppress(Exception):
                importlib.import_module(module)
    # The hard limit sits a whole cap above the last job's soft one: at the soft limit the
    # kernel sends SIGXCPU (parser_timeout), but where soft equals hard it sends SIGKILL,
    # which would read as a crash (a one-job worker's soft limit is ceil(startup) + cpu).
    hard_cpu = cpu * (jobs + 1) + 2
    _limit(resource.RLIMIT_AS, memory, memory)
    _limit(resource.RLIMIT_CORE, 0, 0)
    _limit(resource.RLIMIT_FSIZE, 0, 0)
    _limit(resource.RLIMIT_NPROC, 0, 0)
    out_of_memory = wire.frame({"stage": "error", "error": PARSER_MEMORY})  # built while we can

    def send(data: bytes) -> None:
        out.write(data)
        out.flush()

    send(wire.frame({"stage": "ready", "isolation": isolation}))
    while True:
        job = _read_job(inp)
        if job is None:
            return
        obj, blobs = job
        _limit(resource.RLIMIT_CPU, min(hard_cpu, math.ceil(_cpu_used()) + cpu), hard_cpu)
        try:
            path = obj.get("path")
            data = Path(path).read_bytes() if path else blobs[0]
            blobs = []
            fn = _target(obj["target"])

            def sniffed(found: Sniffed) -> None:
                send(wire.frame({"stage": "sniffed", "sniffed": wire.sniffed_json(found)}))

            parsed = fn(obj["name"], data, obj.get("declared_type"), wire.caps_from(obj["caps"]),
                        frozenset(obj["allow"]), sniffed=sniffed)  # fmt: skip
            reply = wire.parsed_frame(parsed)
            del parsed, data
        except MemoryError:
            send(out_of_memory)
            return  # a worker that hit its cap is discarded
        send(reply)
        del reply


# --- the host side ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Limits:
    time_ms: int
    cpu_s: int
    memory_bytes: int


class _Timeout(Exception):
    pass


class _Closed(Exception):
    pass


class _Worker:
    """One worker process and its pipes."""

    def __init__(self, argv: list[str]) -> None:
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, start_new_session=True,
                                     close_fds=True, env=WORKER_ENV)  # fmt: skip
        self.jobs = 0
        self.isolation: tuple[str, ...] = ()

    def read_frame(self, deadline: float, budget: list[int]) -> tuple[dict[str, Any], list[bytes]]:
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        head = _read_fd(fd, wire.HEADER_BYTES, deadline)
        n = wire.header_length(head)
        _spend(budget, n)
        obj, sizes = wire.load(_read_fd(fd, n, deadline))
        blobs = []
        for size in sizes:
            _spend(budget, size)
            blobs.append(_read_fd(fd, size, deadline))
        return obj, blobs

    def kill(self) -> int | None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            self.proc.kill()
        try:
            code = self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            code = None
        for pipe in (self.proc.stdin, self.proc.stdout):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()
        return code

    def exit_code(self, wait_s: float = 1.0) -> int | None:
        try:
            return self.proc.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            return None


def _spend(budget: list[int], n: int) -> None:
    budget[0] -= n
    if budget[0] < 0:
        raise wire.WireError("the reply is over its cap")


def _read_fd(fd: int, n: int, deadline: float) -> bytes:
    parts: list[bytes] = []
    got = 0
    while got < n:
        left = deadline - time.monotonic()
        if left <= 0:
            raise _Timeout
        ready, _, _ = select.select([fd], [], [], left)
        if not ready:
            raise _Timeout
        chunk = os.read(fd, min(n - got, 1 << 20))
        if not chunk:
            raise _Closed
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)


def _send_job(worker: _Worker, data: bytes) -> threading.Thread:
    def write() -> None:
        stdin = worker.proc.stdin
        if stdin is None:
            return
        with contextlib.suppress(OSError, ValueError):
            stdin.write(data)
            stdin.flush()

    t = threading.Thread(target=write, name="aksha-parser-feed", daemon=True)
    t.start()
    return t


def _signal_of(code: int | None, jailed: bool) -> int | None:
    if code is None:
        return None
    if code < 0:
        return -code
    if jailed and code > 128:  # bubblewrap passes a killed child on as 128 + its signal
        return code - 128
    return None


def _died(code: int | None, jailed: bool, limits: _Limits) -> Gap:
    sig = _signal_of(code, jailed)
    if sig == signal.SIGXCPU:
        return _gap(f"{PARSER_TIMEOUT}: over the CPU cap of {limits.cpu_s} s; "
                    "the parser was stopped")  # fmt: skip
    if sig is not None:
        try:
            how = f"signal {signal.Signals(sig).name}"
        except ValueError:
            how = f"signal {sig}"
    else:
        how = "no exit code" if code is None else f"exit {code}"
    return _gap(f"{PARSER_CRASHED}: the parser process died ({how})")


def _python_paths() -> list[str]:
    """The absolute import path entries that exist: what a worker imports from."""
    seen: list[str] = []
    for entry in sys.path:
        if entry and os.path.isabs(entry) and os.path.exists(entry) and entry not in seen:
            seen.append(entry)
    return seen


def _read_only_paths(python: str, paths: Sequence[str]) -> list[str]:
    """What a jailed worker must see: the interpreter's folder and the import path.

    Every path is resolved first (``python`` and ``paths`` already are), so no symlink
    inside a bound folder points at something the box does not have.
    """
    want = [os.path.dirname(os.path.dirname(python)), *paths]
    found = sorted({os.path.realpath(p) for p in want if p and os.path.exists(p)})
    kept: list[str] = []
    for p in found:  # a path under one already kept is bound with it
        if not any(p == k or p.startswith(k.rstrip("/") + "/") for k in kept):
            kept.append(p)
    return kept


def _close_all(idle: dict[_Limits, list[_Worker]]) -> None:
    for workers in idle.values():
        for w in workers:
            w.kill()
    idle.clear()


class ParserPool:
    """A :data:`~aksha.extensions.ingest.parse.Parse` that runs parsing in worker processes.

    ``size`` idle workers are kept per set of limits, each reused for up to ``max_jobs``
    jobs; ``max_workers`` bound how many parse at once. ``jail`` runs each job in its own box
    (module doc), its input file written under ``scratch``. ``target`` is the function each
    worker runs, as ``module:function`` (the default is the real parsing; tests name a
    hostile one). ``python`` is the interpreter (default: this one).
    """

    def __init__(
        self,
        *,
        size: int = 2,
        max_workers: int = 4,
        max_jobs: int = 50,
        jail: Jail | None = None,
        scratch: Path | None = None,
        target: str = DEFAULT_TARGET,
        python: str | None = None,
    ) -> None:
        """Keep ``size`` idle workers and run at most ``max_workers`` (class doc)."""
        self.size = size
        self.max_jobs = max_jobs
        self.jail = jail
        self.scratch = scratch
        self.target = target
        self.python = python or sys.executable
        self.isolation: tuple[str, ...] = ()
        """What the last worker reported about its isolation (network namespace or not)."""
        self._paths = _python_paths()
        self._slots = threading.BoundedSemaphore(max_workers)
        self._lock = threading.Lock()
        self._idle: dict[_Limits, list[_Worker]] = {}
        self._finalizer = weakref.finalize(self, _close_all, self._idle)

    def close(self) -> None:
        """Stop every idle worker."""
        with self._lock:
            _close_all(self._idle)

    def __call__(
        self, name: str, data: bytes, declared_type: str | None, caps: Caps, allow: frozenset[str]
    ) -> Parsed:
        """Parse ``data`` in a worker (module doc)."""
        limits = _Limits(caps.parser_time_ms, caps.parser_cpu_s, caps.parser_memory_bytes)
        job = {"target": self.target, "name": name, "declared_type": declared_type,
               "caps": wire.caps_json(caps), "allow": sorted(allow)}  # fmt: skip
        with self._slots:
            if self.jail is not None:
                return self._jailed(job, data, caps, limits)
            worker = self._take(limits) or self._start(self._argv(limits, self.max_jobs), limits)
            if isinstance(worker, Gap):
                return failed(None, worker)
            try:
                parsed, keep = self._run(worker, wire.frame(job, [data]), data, caps, limits)
            except BaseException:  # a bug or an interrupt here never leaves a worker running
                worker.kill()
                raise
            worker.jobs += 1
            if keep and worker.jobs < self.max_jobs and self._give(limits, worker):
                return parsed
            worker.kill()
            return parsed

    # --- internals --------------------------------------------------------------------

    def _argv(
        self,
        limits: _Limits,
        max_jobs: int,
        python: str | None = None,
        paths: Sequence[str] | None = None,
    ) -> list[str]:
        config = {"memory_bytes": limits.memory_bytes, "cpu_s": limits.cpu_s,
                  "max_jobs": max_jobs}  # fmt: skip
        return [python or self.python, "-I", "-B", "-c", _BOOT,
                json.dumps(list(self._paths if paths is None else paths)),
                json.dumps(config)]  # fmt: skip

    def _take(self, limits: _Limits) -> _Worker | None:
        with self._lock:
            workers = self._idle.get(limits, [])
            while workers:
                w = workers.pop()
                if w.proc.poll() is None:
                    return w
                w.kill()
        return None

    def _give(self, limits: _Limits, worker: _Worker) -> bool:
        with self._lock:
            workers = self._idle.setdefault(limits, [])
            if len(workers) >= self.size:
                return False
            workers.append(worker)
            return True

    def _start(self, argv: list[str], limits: _Limits, jailed: bool = False) -> _Worker | Gap:
        try:
            worker = _Worker(argv)
        except OSError as e:
            return _gap(f"{PARSER_CRASHED}: the parser process did not start ({e.strerror or e})")
        try:
            obj, _ = worker.read_frame(time.monotonic() + STARTUP_S, [MAX_REPLY_BYTES])
            if obj.get("stage") != "ready":
                raise wire.WireError("no ready frame")
            worker.isolation = wire.strs(obj.get("isolation", []))
            self.isolation = worker.isolation
        except (_Timeout, _Closed, wire.WireError, OSError):
            code = worker.kill()
            return _died(code, jailed, limits)
        return worker

    def _run(
        self,
        worker: _Worker,
        job: bytes,
        data: bytes,
        caps: Caps,
        limits: _Limits,
        jailed: bool = False,
    ) -> tuple[Parsed, bool]:
        """One job: the result, and whether the worker may be reused."""
        deadline = time.monotonic() + limits.time_ms / 1000
        budget = [MAX_REPLY_BYTES + 4 * caps.max_pixels]
        feeder = _send_job(worker, job)
        found: Sniffed | None = None
        try:
            while True:
                obj, blobs = worker.read_frame(deadline, budget)
                stage = obj.get("stage")
                if stage == "sniffed":
                    found = wire.sniffed_from(obj.get("sniffed"))
                elif stage == "parsed":
                    return wire.parsed_from(obj, blobs, data, caps), True
                elif stage == "error" and obj.get("error") == PARSER_MEMORY:
                    worker.kill()
                    mib = limits.memory_bytes // (1024 * 1024)
                    return failed(
                        found,
                        _gap(
                            f"{PARSER_MEMORY}: over the memory cap of "
                            f"{mib:,} MiB; the parser was stopped"
                        ),
                    ), False
                else:
                    raise wire.WireError(f"an unexpected frame: {stage!r}")
        except _Timeout:
            worker.kill()
            return failed(found, _gap(f"{PARSER_TIMEOUT}: no result within {limits.time_ms:,} "
                                      "ms; the parser was stopped")), False  # fmt: skip
        except _Closed:
            code = worker.exit_code()
            worker.kill()
            return failed(found, _died(code, jailed, limits)), False
        except (ValueError, TypeError, KeyError, OSError):  # WireError, or a field decoder
            worker.kill()  # that met a shape it does not check: the reply is untrusted
            return failed(
                found,
                _gap(f"{PARSER_CRASHED}: the parser sent a reply the protocol does not allow"),
            ), False
        finally:
            feeder.join(timeout=5)

    def _jailed(self, job: dict[str, Any], data: bytes, caps: Caps, limits: _Limits) -> Parsed:
        assert self.jail is not None
        folder = Path(tempfile.mkdtemp(prefix="aksha-parse-", dir=self.scratch))
        try:
            host = folder / "document"
            host.write_bytes(data)
            host.chmod(0o400)
            # Resolved paths: a virtual environment's interpreter is a symlink, and the import
            # path is given whole, so the box needs only the real files.
            python = os.path.realpath(self.python)
            paths = [os.path.realpath(p) for p in self._paths]
            argv = self.jail(self._argv(limits, 1, python, paths),
                             _read_only_paths(python, paths), str(host), BOX_INPUT)  # fmt: skip
            worker = self._start(argv, limits, jailed=True)
            if isinstance(worker, Gap):
                return failed(None, worker)
            self.isolation = (JAILED,)
            try:
                parsed, _ = self._run(worker, wire.frame({**job, "path": BOX_INPUT}), data,
                                      caps, limits, jailed=True)  # fmt: skip
            finally:
                worker.kill()
            return parsed
        finally:
            with contextlib.suppress(OSError):
                (folder / "document").chmod(0o600)
            shutil.rmtree(folder, ignore_errors=True)
