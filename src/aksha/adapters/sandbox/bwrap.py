"""``BwrapSandbox``: the ``os-jail`` backend, one bubblewrap box per exec (ADR-P12).

Every exec is its own box, and a request that names a session runs in that session's
persistent workspace (decision 37, :mod:`aksha.adapters.sandbox.workspace`): the folder is
bound as ``/work`` instead of a fresh one, so files one exec writes are there for the next.
The inputs are written over it, nothing else is removed, the session's boxes run one at a
time, and after the box only the files this exec wrote or changed are collected (or every
file, for ``collect="all"``). The budgets on the writable paths count what the exec adds to
what was there. A git workspace is checkpointed after each exec. The folder is kept after
the run; ``clean`` removes it. A backend built without a workspaces root runs every box
fresh.

What a box gets (decisions 3, 4, 7):

- **An allowlisted root**: ``/usr`` and the ``/bin``, ``/sbin`` and ``/lib*`` links or
  folders beside it, read-only; from ``/etc`` only the loader's files; a fresh ``/proc``,
  ``/dev`` and a size-capped ``/tmp``. Not the host's ``/``: no home, no repository, no
  store file.
- **No network**: every namespace is unshared (``--unshare-all``), so the box has only its
  own loopback. A policy with the network on is refused.
- **A ``/work``**: the session's workspace folder, or for a request with no session a
  fresh folder under ``scratch``; either way the workspace tree (blobs read through the
  reader given) and the inline files are written into it. ``/work`` is read-only except the
  policy's writable paths (``.`` is all of it); a protected path is bound read-only again
  over a writable one. An environment image is bound read-only at ``/env``, its ``bin``
  first on ``PATH``.
- **A cleared environment** from a fixed base (``PATH``, ``HOME``, ``TMPDIR``, ``LANG``), so
  no key reaches the box (T2).
- **Hard limits** through ``prlimit`` inside the box (CPU, address space, processes, open
  files, file size, no core files), so the process limit counts the box's own user
  namespace, not every process of the host user. The wall time is the host's: at the
  deadline the box's process group is killed, and bubblewrap's ``--die-with-parent`` and
  its PID namespace take every child with it.
- **Budgets for the whole box while it runs.** ``prlimit`` limits are per process, and the
  writable paths are host folders that bubblewrap's ``--size`` does not cap. A watcher on
  the host samples the box every ``WATCH_INTERVAL_S``: the private memory of every process
  in it (``RssAnon`` summed over bubblewrap's descendants) against ``memory_mb``, and the
  space and entries under the writable paths against ``max_collect_bytes`` and
  ``MAX_WORK_ENTRIES``. A breach kills the box at once: memory comes back ``exec_limit``,
  storage ``output_rejected``, each with a denial. The bound is sampled, not exact: between
  two samples a program can go over by what it allocates or writes in one interval, each
  file still capped at ``max_file_bytes`` and each process at ``memory_mb``.

After the program exits, every regular file under a writable path is collected (for a
session box, every one it wrote or changed), never following a symlink (each one skipped
is a ``symlink`` denial), up to ``max_collect_bytes`` (``output_rejected`` past it), and a
fresh box's folder is deleted: the backend keeps nothing of a box with no session.
Blocked actions found on stderr come back as denials. The probe runs one trivial box once
per backend object; a host without ``bwrap`` or ``prlimit``, or with unprivileged user
namespaces off, probes unavailable and every run is refused ``sandbox_unavailable``: there
is no unconfined fallback.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import IO

from aksha.adapters.sandbox.process import _group_alive
from aksha.adapters.sandbox.workspace import SessionFolders
from aksha.runtime.sandbox.errors import SANDBOX_UNAVAILABLE, SandboxRefused
from aksha.runtime.sandbox.ports import BlobReader, UnitHolder
from aksha.runtime.sandbox.types import (
    Availability,
    BoxRequest,
    BoxResult,
    Denial,
    EnvironmentMount,
    ExecPolicy,
    ExecStatus,
    Limits,
    SessionRef,
    Strength,
    TreeManifest,
)

NAME = "bwrap"
PROBE_TIMEOUT_S = 10.0
SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
ETC_FILES = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/alternatives",
             "/etc/localtime")  # fmt: skip
PRLIMIT = "/usr/bin/prlimit"
HOST_ENV = {"PATH": "/usr/bin:/bin"}
"""The environment bubblewrap itself starts with; ``--clearenv`` clears it for the box."""
BASE_PATH = "/usr/local/bin:/usr/bin:/bin"
WATCH_INTERVAL_S = 0.02
LARGE_WORK_ENTRIES = 2_000
"""A session workspace holding more entries than this is sampled every ``LARGE_WATCH_S``."""
LARGE_WATCH_S = 0.25
MAX_WORK_ENTRIES = 10_000
"""The most files and folders the writable paths may hold while a box runs."""
SIGNAL_LIMITS = frozenset({128 + signal.SIGXCPU, 128 + signal.SIGXFSZ})
_DENIALS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("write", re.compile(rb"Read-only file system|Permission denied")),
    (
        "connect",
        re.compile(
            rb"Network is unreachable|Temporary failure in name resolution|"
            rb"Name or service not known|Connection refused"
        ),
    ),
    ("process", re.compile(rb"Cannot fork|Resource temporarily unavailable|fork: retry")),
    ("limit", re.compile(rb"MemoryError|Cannot allocate memory|File size limit exceeded")),
)


def _version_tuple(text: str) -> tuple[int, ...]:
    found = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(g or 0) for g in found.groups()) if found else ()


class _Box:
    """The running box as a ``KillableUnit``: its host-side process group."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc

    def _signal(self, sig: signal.Signals) -> None:
        self._proc.poll()
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._proc.pid, sig)

    def request_settle(self) -> None:
        self._signal(signal.SIGTERM)

    def terminate(self) -> None:
        self._signal(signal.SIGKILL)

    def settled(self) -> bool:
        return self._proc.poll() is not None and not _group_alive(self._proc.pid)

    def wait(self, seconds: float) -> bool:
        try:
            self._proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            return False
        return self.settled()


class _Capped:
    """Reads a pipe to the end on a thread, keeping at most ``cap`` bytes."""

    def __init__(self, pipe: IO[bytes], cap: int) -> None:
        self.data = bytearray()
        self.truncated = False
        self._pipe, self._cap = pipe, cap
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            while chunk := self._pipe.read(65_536):
                room = self._cap - len(self.data)
                if room > 0:
                    self.data.extend(chunk[:room])
                if len(chunk) > max(room, 0):
                    self.truncated = True


def _descendants(root: int) -> list[int]:
    """``root`` and every process below it, from ``/proc``.

    Orphans in the box reparent to the box's own init, which is below ``root``.
    """
    children: dict[int, list[int]] = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        with contextlib.suppress(OSError, ValueError, IndexError):
            stat_line = Path(entry.path, "stat").read_text()
            ppid = int(stat_line[stat_line.rindex(")") + 2 :].split()[1])
            children.setdefault(ppid, []).append(int(entry.name))
    out, todo = [], [root]
    while todo:
        pid = todo.pop()
        out.append(pid)
        todo.extend(children.get(pid, ()))
    return out


def _anon_bytes(pid: int) -> int:
    with contextlib.suppress(OSError, ValueError):
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("RssAnon:"):
                return int(line.split()[1]) * 1024
    return 0


def _work_usage(roots: list[Path], max_entries: int) -> tuple[int, int]:
    """Bytes allocated and entries under ``roots``, never following a symlink.

    The walk stops early once it is past ``max_entries``.
    """
    used = entries = 0
    for top in roots:
        if top.is_symlink() or not top.is_dir():
            continue
        for folder, dirs, names in os.walk(top, followlinks=False):
            for name in (*dirs, *names):
                entries += 1
                with contextlib.suppress(OSError):
                    used += os.lstat(os.path.join(folder, name)).st_blocks * 512
            if entries > max_entries:
                return used, entries
    return used, entries


class _Watch:
    """Samples a running box's memory and writable storage; kills it on a breach (module doc)."""

    def __init__(
        self,
        box: _Box,
        pid: int,
        roots: list[Path],
        limits: Limits,
        base: tuple[int, int] = (0, 0),
    ) -> None:
        self.breach: Denial | None = None
        self._box, self._pid, self._roots, self._limits = box, pid, roots, limits
        self._base = base
        self._interval = LARGE_WATCH_S if base[1] > LARGE_WORK_ENTRIES else WATCH_INTERVAL_S
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.thread.join(5)

    def _sample(self) -> Denial | None:
        memory = self._limits.memory_mb * 1_048_576
        anon = sum(_anon_bytes(pid) for pid in _descendants(self._pid))
        if anon > memory:
            return Denial("limit", f"box memory {anon} bytes over {memory}")
        cap = self._limits.max_collect_bytes
        base_used, base_entries = self._base
        used, entries = _work_usage(self._roots, base_entries + MAX_WORK_ENTRIES)
        if used - base_used > cap:
            return Denial("output", f"writable paths hold {used - base_used} bytes, over {cap}")
        if entries - base_entries > MAX_WORK_ENTRIES:
            return Denial("output", f"writable paths hold over {MAX_WORK_ENTRIES} entries")
        return None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            found = self._sample()
            if found is not None:
                self.breach = found
                self._box.terminate()
                return


def _writable_roots(work: Path, policy: ExecPolicy) -> list[Path]:
    return [work] if "." in policy.writable else [work / w for w in policy.writable]


class BwrapSandbox:
    """The bubblewrap backend (module doc)."""

    name = NAME
    strength: Strength = "os-jail"

    def __init__(
        self,
        *,
        binary: str | None = None,
        scratch: Path | None = None,
        prlimit: str = PRLIMIT,
        workspaces: Path | None = None,
        origin: Path | None = None,
    ) -> None:
        """Use ``binary`` (default: ``bwrap`` on ``PATH``); make fresh boxes under ``scratch``.

        ``workspaces`` is the root of the session workspaces, each started from ``origin``
        (a git repository, a folder, or None for empty); without it every box is fresh.
        """
        self._binary = binary or shutil.which("bwrap") or "bwrap"
        self._scratch = scratch
        self.folders = None if workspaces is None else SessionFolders(workspaces, origin)
        self._prlimit = prlimit
        self._probe: Availability | None = None
        self._version = ""
        self._lock = threading.Lock()

    # --- probe ------------------------------------------------------------------------

    def probe(self) -> Availability:
        """Run one trivial box, once per backend object (module doc)."""
        with self._lock:
            if self._probe is None:
                self._probe = self._probe_now()
            return self._probe

    def _probe_now(self) -> Availability:
        def no(reason: str) -> Availability:
            return Availability(NAME, self.strength, False, reason)

        if not Path(self._binary).is_file() or not os.access(self._binary, os.X_OK):
            return no(f"{self._binary!r} is not an executable (install bubblewrap)")
        if not Path(self._prlimit).is_file():
            return no(f"{self._prlimit} is missing (util-linux); limits cannot be enforced")
        try:
            done = subprocess.run([self._binary, "--version"], capture_output=True, text=True,
                                  timeout=PROBE_TIMEOUT_S, env=HOST_ENV)  # fmt: skip
            self._version = done.stdout.strip() or done.stderr.strip()
            argv = [*self._base_args(Limits()), "--", "/usr/bin/true"]
            ran = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=PROBE_TIMEOUT_S, env=HOST_ENV)  # fmt: skip
        except (OSError, subprocess.TimeoutExpired) as exc:
            return no(f"probe failed: {type(exc).__name__}: {exc}")
        if ran.returncode != 0:
            first = (ran.stderr.strip().splitlines() or ["no output"])[0]
            return no(f"a trivial box failed ({ran.returncode}): {first}")
        return Availability(NAME, self.strength, True)

    def version(self) -> str:
        """``bwrap --version`` as probed."""
        self.probe()
        return self._version or "unknown"

    # --- one box ----------------------------------------------------------------------

    def run(self, request: BoxRequest, blobs: BlobReader, unit: UnitHolder) -> BoxResult:
        """Run one box (module doc); a fresh box's folder is deleted whatever happens."""
        probe = self.probe()
        if not probe.available:
            raise SandboxRefused(SANDBOX_UNAVAILABLE, f"bwrap: {probe.reason}")
        policy = request.policy
        if policy.network != "none":
            raise SandboxRefused("network_unsupported", "bwrap boxes run with the network off")
        if request.session is not None and self.folders is not None:
            with self.folders.lock(request.session):
                return self._run_in_session(request, request.session, blobs, unit)
        if self._scratch is not None:
            self._scratch.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="aksha-box-", dir=self._scratch))
        try:
            work = root / "work"
            work.mkdir(parents=True)
            return self._box_in(work, root, request, blobs, unit)
        finally:
            _remove(root)

    def _run_in_session(
        self, request: BoxRequest, ref: SessionRef, blobs: BlobReader, unit: UnitHolder
    ) -> BoxResult:
        """One box in the session's workspace: kept, checkpointed, recorded (module doc)."""
        assert self.folders is not None
        work, meta, created = self.folders.ensure(ref)
        if self._scratch is not None:
            self._scratch.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="aksha-env-", dir=self._scratch))
        try:
            result = self._box_in(work, root, request, blobs, unit, session=meta)
        finally:
            _remove(root)
        record: dict[str, object] = {
            "backend": NAME,
            "folder": meta["folder"],
            "created": created,
            "origin": _shown(meta["origin"]),
        }
        commit = self.folders.checkpoint(meta, f"aksha: {request.invocation_id}")
        if commit is not None:
            record["checkpoint"] = commit
        return replace(result, workspace=record)

    def sessions(self) -> tuple[SessionRef, ...]:
        """Every session with a workspace folder."""
        return () if self.folders is None else self.folders.sessions()

    def clean(self, ref: SessionRef) -> Mapping[str, object] | None:
        """Remove ``ref``'s workspace folder; a git workspace's branch and commits stay."""
        return None if self.folders is None else self.folders.clean(ref)

    def _box_in(
        self,
        work: Path,
        root: Path,
        request: BoxRequest,
        blobs: BlobReader,
        unit: UnitHolder,
        *,
        session: Mapping[str, object] | None = None,
    ) -> BoxResult:
        policy = request.policy
        fixed = () if session is None or self.folders is None else self.folders.protected(session)
        inputs = (*(request.tree.entries if request.tree is not None else ()), *request.files)
        for rel in inputs:
            if any(rel == p or rel.startswith(p + "/") for p in fixed):
                raise SandboxRefused("exec_invalid", f"input {rel!r} would overwrite the "
                                     "workspace's own git file")  # fmt: skip
        _write_inputs(work, request.tree, request.files, blobs)
        for path in policy.writable:
            if path != ".":
                (work / path).mkdir(parents=True, exist_ok=True)
        env_dir = None
        if request.environment is not None:
            env_dir = root / "env"
            _materialise_env(env_dir, request.environment, blobs)
        argv = self._command(request, work, env_dir, fixed)
        before = None
        if session is not None and request.collect == "changed":
            before = _signatures(_writable_roots(work, policy))
        return self._spawn(argv, request, work, unit, fixed=fixed, before=before,
                           stateful=session is not None)  # fmt: skip

    def _base_args(self, limits: Limits) -> list[str]:
        args = [self._binary, "--unshare-all", "--die-with-parent", "--new-session",
                "--clearenv", "--hostname", "aksha-box"]  # fmt: skip
        for path in SYSTEM_DIRS:
            p = Path(path)
            if p.is_symlink():
                args += ["--symlink", os.readlink(p), path]
            elif p.is_dir():
                args += ["--ro-bind", path, path]
        for path in ETC_FILES:
            args += ["--ro-bind-try", path, path]
        args += ["--proc", "/proc", "--dev", "/dev"]
        if _version_tuple(self._version) >= (0, 10):
            args += ["--size", str(limits.max_collect_bytes)]
        args += ["--tmpfs", "/tmp"]
        return args

    def jail_argv(
        self, argv: Sequence[str], read_only: Sequence[str], input_file: str, box_input: str
    ) -> list[str]:
        """``argv`` in a box that sees the system, ``read_only`` and one file, all read-only.

        For the ingest extension's parser worker (``Jail`` in
        :mod:`aksha.extensions.ingest.worker`): the allowlisted root and a fresh ``/tmp`` as
        for every box, no network (every namespace unshared), a cleared environment, each
        ``read_only`` path bound read-only at its own path (the interpreter and its import
        path) and ``input_file`` read-only at ``box_input``. No ``prlimit``: the worker sets
        its own limits before it reads the input, and the host keeps the wall clock.
        """
        args = self._base_args(Limits())
        for path in read_only:
            args += ["--ro-bind", path, path]
        args += ["--ro-bind", input_file, box_input, "--chdir", "/tmp",
                 "--setenv", "PATH", BASE_PATH, "--setenv", "HOME", "/tmp",
                 "--setenv", "LANG", "C.UTF-8", "--", *argv]  # fmt: skip
        return args

    def _command(
        self,
        request: BoxRequest,
        work: Path,
        env_dir: Path | None,
        fixed: tuple[str, ...] = (),
    ) -> list[str]:
        policy = request.policy
        limits = policy.limits
        args = self._base_args(limits)
        args += ["--ro-bind", str(work), "/work"]
        for path in policy.writable:
            host = work if path == "." else work / path
            args += ["--bind", str(host), "/work" if path == "." else f"/work/{path}"]
        for path in (*policy.protected, *fixed):
            if (work / path).exists() or (work / path).is_symlink():
                args += ["--ro-bind", str(work / path), f"/work/{path}"]
        path_var = BASE_PATH
        if env_dir is not None:
            args += ["--ro-bind", str(env_dir), "/env"]
            path_var = f"/env/bin:{BASE_PATH}"
        args += ["--chdir", "/work", "--setenv", "PATH", path_var, "--setenv", "HOME", "/tmp",
                 "--setenv", "TMPDIR", "/tmp", "--setenv", "LANG", "C.UTF-8"]  # fmt: skip
        args += ["--", self._prlimit,
                 f"--cpu={limits.cpu_s}:{limits.cpu_s + 1}",
                 f"--as={limits.memory_mb * 1_048_576}",
                 f"--nproc={limits.max_processes}",
                 f"--nofile={limits.max_open_files}",
                 f"--fsize={limits.max_file_bytes}",
                 "--core=0", "--", *request.argv]  # fmt: skip
        return args

    def _spawn(
        self,
        argv: list[str],
        request: BoxRequest,
        work: Path,
        unit: UnitHolder,
        *,
        fixed: tuple[str, ...] = (),
        before: Mapping[str, tuple[int, ...]] | None = None,
        stateful: bool = False,
    ) -> BoxResult:
        limits = request.policy.limits
        roots = _writable_roots(work, request.policy)
        base = _work_usage(roots, 10**9) if stateful else (0, 0)
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True,
                                close_fds=True, env=HOST_ENV)  # fmt: skip
        box = _Box(proc)
        unit.attach(box)
        timed_out = False
        watch = _Watch(box, proc.pid, roots, limits, base)
        try:
            out = _Capped(_pipe(proc.stdout), limits.max_output_bytes)
            err = _Capped(_pipe(proc.stderr), limits.max_output_bytes)
            # stdin is fed on its own thread, so a program that never reads it cannot hold
            # the host past the deadline.
            feeder = threading.Thread(target=_feed, args=(proc.stdin, request.stdin),
                                      daemon=True)  # fmt: skip
            feeder.start()
            try:
                proc.wait(timeout=limits.wall_ms / 1000)
            except subprocess.TimeoutExpired:
                timed_out = True
                box.terminate()
                proc.wait()
            watch.stop()
            if _group_alive(proc.pid):  # anything the program left running goes too
                box.terminate()
            out.thread.join(5)
            err.thread.join(5)
            feeder.join(5)
        finally:
            watch.stop()
            unit.detach()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
        code = proc.returncode
        stdout, stderr = bytes(out.data), bytes(err.data)
        denials = list(_denials(stderr))
        breach = watch.breach
        if breach is not None:
            denials.append(breach)
            timed_out = False  # the breach killed the box, not the deadline
        collected: dict[str, bytes]
        if breach is not None and breach.kind == "output":
            collected, rejected = {}, True
        else:
            collected, rejected = _collect(work, request.policy, denials, fixed, before)
        status: ExecStatus
        if breach is not None:
            status = "exec_limit" if breach.kind == "limit" else "output_rejected"
        elif timed_out:
            status = "exec_timeout"
        elif rejected:
            status = "output_rejected"
        elif code in SIGNAL_LIMITS or (any(d.kind == "limit" for d in denials) and code != 0):
            status = "exec_limit"
        elif code == 0:
            status = "ok"
        else:
            status = "exec_failed"
        if rejected:
            collected = {}
        return BoxResult(status, None if timed_out else code, stdout, stderr, tuple(denials),
                         collected, out.truncated, err.truncated)  # fmt: skip


def _pipe(stream: IO[bytes] | None) -> IO[bytes]:
    assert stream is not None
    return stream


def _feed(stdin: IO[bytes] | None, data: bytes) -> None:
    if stdin is None:
        return
    with contextlib.suppress(OSError, ValueError):  # a closed or broken pipe ends the feed
        if data:
            stdin.write(data)
        stdin.close()


def _write(base: Path, rel: str, data: bytes) -> None:
    target = base
    for part in Path(rel).parts[:-1]:
        target = target / part
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            target.unlink()
        target.mkdir(exist_ok=True)
    target = target / Path(rel).name
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        _remove(target)
    target.write_bytes(data)


def _write_inputs(
    work: Path, tree: TreeManifest | None, files: Mapping[str, bytes], blobs: BlobReader
) -> None:
    """Into ``/work``: the tree's blobs, then the inline files over them.

    In a session workspace a path the box made a symlink or a folder is replaced, never
    followed, so an input cannot be written outside the workspace.
    """
    if tree is not None:
        for rel, ref in tree.entries.items():
            _write(work, rel, blobs.get(ref))
    for rel, data in files.items():
        _write(work, rel, data)


def _materialise_env(env_dir: Path, mount: EnvironmentMount, blobs: BlobReader) -> None:
    """The image's files under ``env_dir``; ``bin/`` entries are made executable."""
    env_dir.mkdir(parents=True)
    for rel, ref in mount.tree.entries.items():
        _write(env_dir, rel, blobs.get(ref))
        if rel.startswith("bin/"):
            (env_dir / rel).chmod(0o755)


def _denials(stderr: bytes) -> list[Denial]:
    out: list[Denial] = []
    for line in stderr.splitlines():
        for kind, pattern in _DENIALS:
            if pattern.search(line):
                out.append(Denial(kind, line.decode("utf-8", "replace")[:200]))  # type: ignore[arg-type]
                break
    return out[:20]


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)


def _signatures(roots: list[Path]) -> dict[str, tuple[int, ...]]:
    """Each file or symlink under ``roots`` (absolute path) to what says it changed."""
    out: dict[str, tuple[int, ...]] = {}
    for top in roots:
        if top.is_symlink() or not top.is_dir():
            continue
        for folder, dirs, names in os.walk(top, followlinks=False):
            for name in (*dirs, *names):
                path = os.path.join(folder, name)
                with contextlib.suppress(OSError):
                    info = os.lstat(path)
                    if not stat.S_ISDIR(info.st_mode):
                        out[path] = _signature(info)
    return out


def _shown(origin: Mapping[str, object]) -> dict[str, object]:
    """A workspace origin as an exec records it (the git directory is the backend's own)."""
    return {k: v for k, v in origin.items() if k != "gitdir"}


def _collect(
    work: Path,
    policy: ExecPolicy,
    denials: list[Denial],
    fixed: tuple[str, ...] = (),
    before: Mapping[str, tuple[int, ...]] | None = None,
) -> tuple[dict[str, bytes], bool]:
    """Every regular file under a writable path, symlink-safe and capped (module doc).

    With ``before``, only the files whose signature differs from it (written or changed).
    """
    out: dict[str, bytes] = {}
    total = 0
    protected = (*policy.protected, *fixed)
    for top in _writable_roots(work, policy):
        if top.is_symlink() or not top.is_dir():
            continue
        for folder, dirs, names in os.walk(top, followlinks=False):
            base = Path(folder)
            for d in list(dirs):
                if (base / d).is_symlink():
                    dirs.remove(d)
                    if before is not None and str(base / d) in before:
                        continue  # left by an earlier exec of the session, reported then
                    denials.append(Denial("symlink", f"skipped {(base / d).relative_to(work)}"))
            for name in names:
                path = base / name
                rel = path.relative_to(work).as_posix()
                if any(rel == p or rel.startswith(p + "/") for p in protected):
                    continue
                info = path.lstat()
                if before is not None and before.get(str(path)) == _signature(info):
                    continue  # unchanged by this exec
                if stat.S_ISLNK(info.st_mode):
                    denials.append(Denial("symlink", f"skipped {rel}"))
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                total += info.st_size
                if total > policy.limits.max_collect_bytes:
                    denials.append(Denial("output", f"collected output over "
                                          f"{policy.limits.max_collect_bytes} bytes"))  # fmt: skip
                    return {}, True
                with contextlib.suppress(OSError):
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, "rb") as fh:
                        out[rel] = fh.read(policy.limits.max_collect_bytes + 1)
    return out, False


def _remove(root: Path) -> None:
    """Delete the box's folder, even where the program took away permissions."""

    def unlock(func: Callable[..., object], path: str, _exc: object) -> None:
        # Never chmod through a symlink the program left: it would reach outside the box.
        with contextlib.suppress(OSError):
            if os.path.abspath(path) != os.path.abspath(root):
                os.chmod(os.path.dirname(path), 0o700)
            if not os.path.islink(path):
                os.chmod(path, 0o700)
            func(path)

    shutil.rmtree(root, onexc=unlock)
