"""The bubblewrap backend (ADR-P12): the conformance suite against a real box.

Every test in the suite runs a real program in a real box, and is skipped, with the
probe's reason, on a host where the backend does not probe available (no ``bwrap``, or
unprivileged user namespaces turned off). The tests that need no box (a missing binary
refuses rather than runs) always run.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from aksha.adapters.blobs.filesystem import FilesystemBlobStore
from aksha.adapters.sandbox.bwrap import BwrapSandbox
from aksha.runtime.sandbox.errors import SandboxRefused
from aksha.runtime.sandbox.exec import ExecUnit
from aksha.runtime.sandbox.types import (
    BoxRequest,
    BoxResult,
    EnvironmentMount,
    ExecPolicy,
    Limits,
    TreeManifest,
)

BACKEND = BwrapSandbox()
PROBE = BACKEND.probe()
needs_box = pytest.mark.skipif(not PROBE.available, reason=f"bwrap: {PROBE.reason}")
QUICK = Limits(wall_ms=10_000)


@pytest.fixture
def blobs(tmp_path: Path) -> FilesystemBlobStore:
    return FilesystemBlobStore(tmp_path / "blobs")


def _run(
    argv: tuple[str, ...],
    *,
    policy: ExecPolicy | None = None,
    files: dict[str, bytes] | None = None,
    blobs: FilesystemBlobStore | None = None,
    tree: TreeManifest | None = None,
    environment: EnvironmentMount | None = None,
    unit: ExecUnit | None = None,
    backend: BwrapSandbox = BACKEND,
) -> BoxResult:
    request = BoxRequest("test", argv, policy or ExecPolicy.of(limits=QUICK), files=files or {},
                         tree=tree, environment=environment)  # fmt: skip
    return backend.run(request, blobs if blobs is not None else NoBlobs(), unit or ExecUnit())


class NoBlobs:
    def get(self, ref: str) -> bytes:
        raise KeyError(ref)


def sh(script: str) -> tuple[str, ...]:
    return ("sh", "-c", script)


# --- always: the backend refuses rather than pretends ---------------------------------------


def test_a_missing_binary_probes_unavailable_and_refuses_to_run(tmp_path: Path) -> None:
    gone = BwrapSandbox(binary=str(tmp_path / "no-bwrap"))
    probe = gone.probe()
    assert not probe.available and probe.backend == "bwrap" and probe.reason
    with pytest.raises(SandboxRefused) as refused:
        _run(("true",), backend=gone)
    assert refused.value.code == "sandbox_unavailable"


def test_the_probe_is_cached_per_backend() -> None:
    assert BACKEND.probe() is BACKEND.probe()


def test_a_network_allowlist_is_refused_at_the_backend_too() -> None:
    with pytest.raises(SandboxRefused) as refused:
        _run(("true",), policy=ExecPolicy.of(network="allowlist"))
    assert refused.value.code in ("network_unsupported", "sandbox_unavailable")


# --- the conformance suite ------------------------------------------------------------------


@needs_box
def test_a_program_runs_in_work_and_its_output_comes_back() -> None:
    got = _run(sh("pwd; cat in.txt; echo made > out/r.txt"), files={"in.txt": b"hello"})
    assert got.status == "ok" and got.exit_code == 0
    assert got.stdout.decode().split() == ["/work", "hello"]
    assert dict(got.collected) == {"out/r.txt": b"made\n"}


@needs_box
def test_exec_env_has_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """T2: the box's environment is built from a fixed base; no key reaches it."""
    monkeypatch.setenv("AKSHA_TEST_PROVIDER_KEY", "sk-should-never-be-seen")
    got = _run(("env",))
    assert got.status == "ok"
    assert b"sk-should-never-be-seen" not in got.stdout
    names = {line.split("=", 1)[0] for line in got.stdout.decode().splitlines()}
    assert names <= {"PATH", "HOME", "TMPDIR", "LANG", "PWD", "SHLVL", "_"}


@needs_box
def test_the_root_is_an_allowlist_not_the_host(tmp_path: Path) -> None:
    """Decision 3: the host's home, this repository and the store are not in the box."""
    secret = tmp_path / "answer-key.txt"
    secret.write_text("SYN-ANSWER")
    here = Path(__file__).resolve()
    got = _run(sh(f"cat {secret}; cat {here}; ls /home; ls /root; cat /etc/passwd; echo end"))
    assert b"SYN-ANSWER" not in got.stdout and b"conformance" not in got.stdout
    assert got.stdout.strip().endswith(b"end")


@needs_box
def test_the_network_is_off(tmp_path: Path) -> None:
    """Decision 4 and T3: a connect from the box, even to a host listener, fails."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        script = (
            "import socket\n"
            "for host in ('127.0.0.1', '1.1.1.1'):\n"
            "    try:\n"
            f"        socket.create_connection((host, {port}), 2); print('reached')\n"
            "    except OSError as e: print('refused', e.errno)\n"
        )
        got = _run(("python3", "-c", script))
    finally:
        listener.close()
    assert b"reached" not in got.stdout and got.stdout.count(b"refused") == 2
    assert any(d.kind == "connect" for d in got.denials) or got.status == "ok"


@needs_box
def test_writes_outside_the_writable_paths_are_denied() -> None:
    got = _run(sh("echo x > src.txt; echo y > out/ok.txt; echo z > tmp/t.txt"),
               files={"keep.txt": b"k"})  # fmt: skip
    assert dict(got.collected) == {"out/ok.txt": b"y\n", "tmp/t.txt": b"z\n"}
    assert any(d.kind == "write" for d in got.denials)


@needs_box
def test_a_protected_path_stays_read_only_under_a_writable_tree(
    blobs: FilesystemBlobStore,
) -> None:
    tree = TreeManifest({"a.py": blobs.put(b"x = 1\n"), ".git/HEAD": blobs.put(b"ref\n")})
    policy = ExecPolicy.of(writable=(".",), protected=(".git",), limits=QUICK)
    got = _run(sh("echo 'x = 2' > a.py; echo evil > .git/HEAD; echo new > b.py"),
               policy=policy, blobs=blobs, tree=tree)  # fmt: skip
    assert got.collected["a.py"] == b"x = 2\n" and got.collected["b.py"] == b"new\n"
    assert ".git/HEAD" not in got.collected
    assert any(d.kind == "write" for d in got.denials)


@needs_box
def test_the_deadline_kills_the_whole_process_group() -> None:
    """Decision 7: at the deadline the group is killed, children included."""
    marker = "31.4159"
    started = time.monotonic()
    got = _run(sh(f"sleep {marker} & sleep {marker} & wait"),
               policy=ExecPolicy.of(limits=Limits(wall_ms=500)))  # fmt: skip
    assert got.status == "exec_timeout" and time.monotonic() - started < 10
    time.sleep(0.3)
    left = subprocess.run(["pgrep", "-f", f"sleep {marker}"], capture_output=True, text=True)
    assert left.stdout.strip() == ""


@needs_box
def test_the_deadline_holds_while_stdin_is_unread() -> None:
    """Decision 7: a program that never reads a large stdin is still killed at the deadline."""
    started = time.monotonic()
    request = BoxRequest("test", ("sleep", "23.45"), ExecPolicy.of(limits=Limits(wall_ms=500)),
                         stdin=b"x" * (8 * 1024 * 1024))  # fmt: skip
    got = BACKEND.run(request, NoBlobs(), ExecUnit())
    assert got.status == "exec_timeout" and time.monotonic() - started < 10


@needs_box
def test_the_unit_ends_a_running_box_on_request() -> None:
    """T6: a stop ends the box without waiting for the program."""
    unit = ExecUnit()
    out: list[BoxResult] = []
    worker = threading.Thread(target=lambda: out.append(_run(("sleep", "27.18"), unit=unit)))
    started = time.monotonic()
    worker.start()
    while unit.settled() and worker.is_alive() and time.monotonic() - started < 5:
        time.sleep(0.02)
    unit.terminate()
    worker.join(10)
    assert out and out[0].status != "ok" and time.monotonic() - started < 10


@needs_box
def test_the_process_limit_holds() -> None:
    policy = ExecPolicy.of(limits=Limits(wall_ms=10_000, max_processes=8))
    got = _run(sh("for i in $(seq 40); do sleep 1 & done; wait"), policy=policy)
    assert got.status != "ok"
    assert any(d.kind in ("process", "limit") for d in got.denials)


@needs_box
def test_the_memory_limit_holds() -> None:
    policy = ExecPolicy.of(limits=Limits(wall_ms=10_000, memory_mb=64))
    got = _run(("python3", "-c", "x = bytearray(400_000_000)"), policy=policy)
    assert got.status == "exec_limit"


@needs_box
def test_the_memory_budget_is_for_the_whole_box_not_each_process() -> None:
    """Decision 7: forked processes each under the per-process cap still meet the box budget."""
    child = ("import time\nb = bytearray(120_000_000)\n"
             "for i in range(0, len(b), 4096): b[i] = 1\ntime.sleep(8)")  # fmt: skip
    script = f"for i in 1 2 3 4; do python3 -c '{child}' & done; wait"
    started = time.monotonic()
    got = _run(sh(script), policy=ExecPolicy.of(limits=Limits(wall_ms=20_000, memory_mb=200)))
    assert got.status == "exec_limit" and time.monotonic() - started < 7
    assert any(d.kind == "limit" and "box memory" in d.detail for d in got.denials)


@needs_box
def test_writable_storage_is_bounded_while_the_program_runs() -> None:
    """Decision 7: the writable paths cannot grow past the collect cap before the exit."""
    policy = ExecPolicy.of(limits=Limits(wall_ms=20_000, max_collect_bytes=4_000_000))
    script = "for i in $(seq 10); do head -c 1000000 /dev/zero > out/f$i; done; sleep 9"
    started = time.monotonic()
    got = _run(sh(script), policy=policy)
    assert got.status == "output_rejected" and got.collected == {}
    assert time.monotonic() - started < 7


@needs_box
def test_writable_entries_are_bounded_while_the_program_runs() -> None:
    script = ("import time\nfor i in range(20_000): open(f'out/e{i}', 'w').close()\n"
              "time.sleep(9)")  # fmt: skip
    started = time.monotonic()
    got = _run(("python3", "-c", script),
               policy=ExecPolicy.of(limits=Limits(wall_ms=20_000)))  # fmt: skip
    assert got.status == "output_rejected" and time.monotonic() - started < 7
    assert any(d.kind == "output" and "entries" in d.detail for d in got.denials)


@needs_box
def test_the_file_size_limit_holds() -> None:
    policy = ExecPolicy.of(limits=Limits(wall_ms=10_000, max_file_bytes=1_000))
    got = _run(sh("head -c 5000 /dev/zero > out/big"), policy=policy)
    assert got.status == "exec_limit"


@needs_box
def test_output_is_capped_and_marked() -> None:
    policy = ExecPolicy.of(limits=Limits(wall_ms=10_000, max_output_bytes=1_000))
    got = _run(sh("head -c 200000 /dev/zero | tr '\\0' a"), policy=policy)
    assert len(got.stdout) == 1_000 and got.stdout_truncated


@needs_box
def test_collection_is_symlink_safe_and_capped() -> None:
    got = _run(sh("ln -s /etc/hostname out/link; echo ok > out/real"))
    assert dict(got.collected) == {"out/real": b"ok\n"}
    assert any(d.kind == "symlink" for d in got.denials)
    policy = ExecPolicy.of(limits=Limits(wall_ms=10_000, max_collect_bytes=100))
    big = _run(sh("head -c 1000 /dev/zero > out/big"), policy=policy)
    assert big.status == "output_rejected" and big.collected == {}


@needs_box
def test_an_environment_image_is_mounted_read_only_at_env(blobs: FilesystemBlobStore) -> None:
    script = b"#!/bin/sh\necho from-env\n"
    tree = TreeManifest({"bin/hello": blobs.put(script)})
    mount = EnvironmentMount("sha256:" + "a" * 64, tree)
    got = _run(sh("hello; echo x > /env/bin/other"), blobs=blobs, environment=mount)
    assert got.stdout.startswith(b"from-env")
    assert any(d.kind == "write" for d in got.denials)


@needs_box
def test_the_box_keeps_nothing(tmp_path: Path) -> None:
    """T2 and decision 5: after the box, its folder is gone."""
    scratch = tmp_path / "boxes"
    backend = BwrapSandbox(scratch=scratch)
    assert _run(sh("echo x > out/a"), backend=backend).status == "ok"
    assert list(scratch.iterdir()) == []
