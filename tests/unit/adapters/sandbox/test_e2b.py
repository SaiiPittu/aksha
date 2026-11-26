"""The E2B backend (decision 37): one remote sandbox per session, against a stubbed service.

Every test talks to :class:`tests.support.e2b_stub.E2BStub` on a local port; nothing here
reaches the real service, and the key is a synthetic value.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.sandbox.e2b import E2BSandbox
from aksha.runtime.sandbox.errors import SandboxRefused
from aksha.runtime.sandbox.exec import ExecUnit
from aksha.runtime.sandbox.types import (
    BoxRequest,
    BoxResult,
    EnvironmentMount,
    ExecPolicy,
    Limits,
    SessionRef,
    TreeManifest,
)
from tests.support.e2b_stub import Box, E2BStub, Ran

KEY = "SYN-e2b-key-0000"
S1 = SessionRef("o", "S-0001")
S2 = SessionRef("o", "S-0002")


class NoBlobs:
    def get(self, ref: str) -> bytes:
        raise KeyError(ref)


@pytest.fixture
def stub() -> Iterator[E2BStub]:
    with E2BStub(KEY) as s:
        yield s


def backend(stub: E2BStub, tmp_path: Path, **kw: object) -> E2BSandbox:
    return E2BSandbox(state_dir=tmp_path / "e2b", env={"E2B_API_KEY": KEY},
                      api_url=stub.api_url, envd_url=stub.envd_url, **kw)  # type: ignore[arg-type]  # fmt: skip


def run(
    box: E2BSandbox,
    argv: tuple[str, ...],
    session: SessionRef | None = S1,
    *,
    files: dict[str, bytes] | None = None,
    policy: ExecPolicy | None = None,
    stdin: bytes = b"",
    unit: ExecUnit | None = None,
) -> BoxResult:
    request = BoxRequest("R-1/t#a1/x1", argv, policy or ExecPolicy.of(), files=files or {},
                         session=session, stdin=stdin)  # fmt: skip
    return box.run(request, NoBlobs(), unit or ExecUnit())


def creates(stub: E2BStub) -> int:
    return sum(1 for m, p, _ in stub.requests if m == "POST" and p == "/sandboxes")


def test_the_probe_needs_the_key_and_makes_no_request(stub: E2BStub, tmp_path: Path) -> None:
    off = E2BSandbox(state_dir=tmp_path, env={}, api_url=stub.api_url, envd_url=stub.envd_url)
    probe = off.probe()
    assert not probe.available and "E2B_API_KEY" in probe.reason
    with pytest.raises(SandboxRefused) as refused:
        run(off, ("true",))
    assert refused.value.code == "sandbox_unavailable"
    on = backend(stub, tmp_path).probe()
    assert on.available and on.strength == "vm" and stub.requests == []


def test_one_sandbox_per_session_reused_and_stateful(stub: E2BStub, tmp_path: Path) -> None:
    def program(box: Box, argv: list[str], stdin: bytes) -> Ran:
        if argv[-1] == "write":
            return Ran(b"wrote\n", writes={"out/state.txt": b"one\n"})
        kept = box.files.get("/home/user/work/out/state.txt", (b"", 0, "f"))[0]
        return Ran(kept, writes={"out/more.txt": b"two\n"})

    stub.program = program
    e2b = backend(stub, tmp_path)
    first = run(e2b, ("sh", "write"))
    assert first.status == "ok" and dict(first.collected) == {"out/state.txt": b"one\n"}
    second = run(e2b, ("sh", "read"))
    assert second.stdout == b"one\n" and dict(second.collected) == {"out/more.txt": b"two\n"}
    assert creates(stub) == 1 and stub.boxes["SYN-sbx-1"].connects == 1
    assert first.workspace is not None and first.workspace["created"] is True
    assert second.workspace == {**first.workspace, "created": False}
    assert first.workspace["sandbox"] == "SYN-sbx-1"
    run(e2b, ("sh", "x"), S2)
    assert creates(stub) == 2 and e2b.sessions() == (S1, S2)


def test_inputs_and_stdin_are_uploaded_and_argv_is_never_a_shell_string(
    stub: E2BStub, tmp_path: Path
) -> None:
    seen: list[tuple[list[str], bytes]] = []

    def program(box: Box, argv: list[str], stdin: bytes) -> Ran:
        seen.append((argv, stdin))
        return Ran()

    stub.program = program
    run(backend(stub, tmp_path), ("python3", "-c", "print('$(rm -rf /)')"),
        files={"in.txt": b"SYN"}, stdin=b"feed")  # fmt: skip
    assert seen == [(["python3", "-c", "print('$(rm -rf /)')"], b"feed")]
    assert stub.boxes["SYN-sbx-1"].files["/home/user/work/in.txt"][0] == b"SYN"
    start = next(p for m, p, _ in stub.requests if p.endswith("/process.Process/Start"))
    assert start.startswith("/envd/SYN-sbx-1/")


def test_the_sandbox_is_created_offline_and_ends_per_the_setting(
    stub: E2BStub, tmp_path: Path
) -> None:
    run(backend(stub, tmp_path, on_end="kill", timeout_s=120), ("true",))
    body = stub.boxes["SYN-sbx-1"].body
    assert body["allow_internet_access"] is False and body["autoPause"] is False
    assert body["timeout"] == 120 and body["metadata"] == {"aksha_owner": "o",
                                                           "aksha_session": "S-0001"}  # fmt: skip
    run(backend(stub, tmp_path / "b"), ("true",))
    assert stub.boxes["SYN-sbx-2"].body["autoPause"] is True


def test_the_key_goes_only_to_the_control_api_and_nowhere_else(
    stub: E2BStub, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    stub.program = lambda b, a, s: Ran(b"out", b"err", writes={"out/r": b"r"})
    e2b = backend(stub, tmp_path)
    got = run(e2b, ("true",))
    for method, path, headers in stub.requests:
        sent_key = headers.get("x-api-key")
        assert (sent_key == KEY) == path.startswith("/sandboxes"), (method, path)
    state = (tmp_path / "e2b" / "o" / "S-0001" / "e2b.json").read_text()
    assert json.loads(state) == {"owner": "o", "session": "S-0001", "sandbox": "SYN-sbx-1"}
    seen = " ".join([state, repr(e2b), e2b.version(), repr(got), caplog.text,
                     json.dumps(got.workspace)])  # fmt: skip
    assert KEY not in seen
    stub.fail_create = True
    with pytest.raises(SandboxRefused) as refused:
        run(e2b, ("true",), S2)
    assert refused.value.code == "backend_failed" and KEY not in refused.value.message
    assert "HTTP 500" in refused.value.message


def test_a_sandbox_gone_while_idle_is_replaced_and_says_so(stub: E2BStub, tmp_path: Path) -> None:
    e2b = backend(stub, tmp_path)
    run(e2b, ("true",))
    stub.boxes["SYN-sbx-1"].alive = False  # killed at its idle timeout
    got = run(e2b, ("true",))
    assert got.workspace is not None and got.workspace["replaced"] is True
    assert got.workspace["sandbox"] == "SYN-sbx-2" and creates(stub) == 2


def test_clean_kills_the_sandbox_and_forgets_it(stub: E2BStub, tmp_path: Path) -> None:
    e2b = backend(stub, tmp_path)
    run(e2b, ("true",))
    assert e2b.clean(S1) == {"owner": "o", "session": "S-0001", "sandbox": "SYN-sbx-1",
                             "killed": True}  # fmt: skip
    assert not stub.boxes["SYN-sbx-1"].alive and e2b.sessions() == ()
    assert e2b.clean(S1) is None


def test_a_request_with_no_session_gets_a_sandbox_killed_afterwards(
    stub: E2BStub, tmp_path: Path
) -> None:
    got = run(backend(stub, tmp_path), ("true",), None)
    assert got.status == "ok" and got.workspace is None
    assert not stub.boxes["SYN-sbx-1"].alive and not (tmp_path / "e2b").exists()


def test_the_deadline_kills_the_remote_process(stub: E2BStub, tmp_path: Path) -> None:
    stub.program = lambda b, a, s: Ran(hang_s=20)
    started = time.monotonic()
    got = run(backend(stub, tmp_path), ("sleep", "99"),
              policy=ExecPolicy.of(limits=Limits(wall_ms=300)))  # fmt: skip
    assert got.status == "exec_timeout" and got.exit_code is None
    assert stub.signals == [("SYN-sbx-1", 4242, "SIGNAL_SIGKILL")]
    assert time.monotonic() - started < 10


def test_collection_reports_symlinks_honours_protected_paths_and_caps(
    stub: E2BStub, tmp_path: Path
) -> None:
    stub.program = lambda b, a, s: Ran(writes={"out/ok": b"ok", "out/keep/x": b"k"},
                                       links=("out/link",))  # fmt: skip
    policy = ExecPolicy.of(writable=("out",), protected=("out/keep",))
    got = run(backend(stub, tmp_path), ("true",), policy=policy)
    assert dict(got.collected) == {"out/ok": b"ok"}
    assert any(d.kind == "symlink" for d in got.denials)
    stub.program = lambda b, a, s: Ran(writes={"out/big": b"x" * 500})
    capped = ExecPolicy.of(limits=Limits(max_collect_bytes=100))
    big = run(backend(stub, tmp_path / "b"), ("true",), policy=capped)
    assert big.status == "output_rejected" and big.collected == {}


def test_a_listing_the_box_forged_cannot_escape_or_beat_the_cap(
    stub: E2BStub, tmp_path: Path
) -> None:
    stub.program = lambda b, a, s: Ran(writes={"out/big": b"x" * 500})
    stub.listing = lambda lines: (
        [line.replace("\t500\t", "\t0\t") for line in lines] + ["f\t0\t./../../etc/passwd\0"]
    )
    capped = ExecPolicy.of(limits=Limits(max_collect_bytes=100))
    got = run(backend(stub, tmp_path), ("true",), policy=capped)
    assert got.status == "output_rejected" and got.collected == {}
    stub.program = lambda b, a, s: Ran(writes={"out/ok": b"ok"})
    clean = run(backend(stub, tmp_path / "c"), ("true",))
    assert dict(clean.collected) == {"out/ok": b"ok"}
    assert any(d.kind == "output" and "not clean" in d.detail for d in clean.denials)


def test_a_failing_program_and_refusals(stub: E2BStub, tmp_path: Path) -> None:
    stub.program = lambda b, a, s: Ran(stderr=b"boom", code=3)
    e2b = backend(stub, tmp_path)
    got = run(e2b, ("false",))
    assert (got.status, got.exit_code, got.stderr) == ("exec_failed", 3, b"boom")
    with pytest.raises(SandboxRefused) as net:
        run(e2b, ("true",), policy=ExecPolicy.of(network="allowlist"))
    assert net.value.code == "network_unsupported"
    mount = EnvironmentMount("sha256:" + "a" * 64, TreeManifest())
    request = BoxRequest("x", ("true",), ExecPolicy.of(), session=S1, environment=mount)
    with pytest.raises(SandboxRefused) as env:
        e2b.run(request, NoBlobs(), ExecUnit())
    assert env.value.code == "environment_unsupported"
