"""``BoxedExec`` (ADR-P12 decisions 2, 4, 5, 8, 9): refusals, outputs, trees and records.

The backend is the recording fake: these tests are about the runtime around the port.
"""

from __future__ import annotations

from pathlib import Path

from aksha.adapters.blobs.filesystem import FilesystemBlobStore
from aksha.runtime.sandbox.exec import BoxedExec, ExecUnit
from aksha.runtime.sandbox.types import (
    BoxRequest,
    BoxResult,
    EnvironmentMount,
    ExecPolicy,
    SandboxNeeds,
    SandboxPolicy,
    TreeManifest,
)
from tests.support.sandbox import FakeSandbox


def _exec(box: FakeSandbox | None, needs: SandboxNeeds | None = None, **kw: object) -> BoxedExec:
    needs = needs or SandboxNeeds()
    probe = None if box is None else box.probe()
    policy = ExecPolicy(needs.policy, needs.limits)
    return BoxedExec(box, probe, needs, policy, invocation="R-1/t/t01/c01#a1", **kw)  # type: ignore[arg-type]


def test_no_backend_refuses_and_spawns_nothing() -> None:
    got = _exec(None).run(["echo", "hi"])
    assert (got.status, got.code) == ("refused", "sandbox_unavailable")
    assert got.record["code"] == "sandbox_unavailable"


def test_an_unavailable_or_weaker_backend_refuses_before_spawning() -> None:
    off = FakeSandbox(available=False)
    assert _exec(off).run(["true"]).code == "sandbox_unavailable" and off.requests == []
    weak = FakeSandbox(strength="os-jail")
    got = _exec(weak, SandboxNeeds(min_strength="vm")).run(["true"])
    assert got.code == "sandbox_unavailable" and weak.requests == []


def test_a_policy_with_the_network_on_is_refused_by_every_shipped_backend() -> None:
    box = FakeSandbox()
    needs = SandboxNeeds(policy=SandboxPolicy(network="allowlist"))
    assert _exec(box, needs).run(["curl"]).code == "network_unsupported" and box.requests == []


def test_a_run_sends_argv_files_and_the_effective_policy_and_records_it() -> None:
    box = FakeSandbox()
    ex = _exec(box)
    got = ex.run(["python3", "main.py"], {"main.py": b"print(1)", "out/r.txt": b"r"})
    (sent,) = box.requests
    assert sent.argv == ("python3", "main.py") and sent.policy == ExecPolicy()
    assert got.ok and got.stdout == "python3 main.py"
    assert dict(got.outputs) == {"out/r.txt": b"r"}  # only the declared outputs come back
    (record,) = ex.records
    assert record["backend"] == "fake" and record["version"] == "fake-1"
    assert record["policy_hash"] == ExecPolicy().hash_ref()
    assert set(record["inputs"]) == {"main.py", "out/r.txt"}
    assert set(record["outputs"]) == {"out/r.txt"} and record["exit_code"] == 0


def test_a_failing_program_is_data_not_an_exception() -> None:
    box = FakeSandbox(rule=lambda r, b: BoxResult("exec_failed", 2, b"", b"boom"))
    got = _exec(box).run(["false"])
    assert (got.status, got.exit_code, got.stderr, got.code) == ("exec_failed", 2, "boom",
                                                                 "exec_failed")  # fmt: skip


def test_a_writable_tree_comes_back_as_a_new_manifest_with_blobs_saved(tmp_path: Path) -> None:
    blobs = FilesystemBlobStore(tmp_path / "blobs")
    old = TreeManifest({"a.py": blobs.put(b"x = 1"), ".git/HEAD": blobs.put(b"ref")})

    def edit(request: BoxRequest, reader: object) -> BoxResult:
        return BoxResult("ok", 0, b"", b"", collected={"a.py": b"x = 2", "b.py": b"y"})

    needs = SandboxNeeds(tree_writable=True,
                         policy=SandboxPolicy(writable=(".",), protected=(".git",)))  # fmt: skip
    ex = _exec(FakeSandbox(rule=edit), needs, blobs=blobs)
    got = ex.run(["sh", "-c", "edit"], tree=old)
    assert got.tree is not None
    assert set(got.tree.entries) == {"a.py", "b.py", ".git/HEAD"}
    assert blobs.get(got.tree.entries["a.py"]) == b"x = 2"
    assert got.tree.entries[".git/HEAD"] == old.entries[".git/HEAD"]
    assert ex.records[0]["tree"] == old.hash_ref()
    assert ex.records[0]["new_tree"] == got.tree.hash_ref()


def test_an_environment_is_mounted_or_its_refusal_is_returned() -> None:
    box = FakeSandbox()
    mount = EnvironmentMount("sha256:" + "e" * 64, TreeManifest())
    needs = SandboxNeeds(environment="py")
    got = _exec(box, needs, environments=lambda n, t, f: (mount, {"hit": True})).run(["pytest"])
    assert got.ok and box.requests[0].environment == mount
    assert got.record["environment"] == {"name": "py", "key": mount.key, "hit": True}
    code = "environment_approval_required"
    refused = _exec(box, needs, environments=lambda n, t, f: (code, {})).run(["x"])
    assert refused.code == code and box.requests[1:] == []


def test_a_widened_policy_refuses_every_run() -> None:
    box = FakeSandbox()
    got = _exec(box, refusal=("policy_widened", "src is not writable")).run(["true"])
    assert got.code == "policy_widened" and box.requests == []


def test_a_backend_bug_is_a_recorded_refusal() -> None:
    def broken(request: BoxRequest, reader: object) -> BoxResult:
        raise RuntimeError("bug")

    got = _exec(FakeSandbox(rule=broken)).run(["true"])
    assert got.code == "backend_error" and "bug" in got.stderr


def test_a_stopped_unit_refuses_every_later_run() -> None:
    unit = ExecUnit()
    box = FakeSandbox()
    ex = _exec(box, unit=unit)
    unit.request_settle()
    assert ex.run(["true"]).code == "run_stopped" and box.requests == []
    assert unit.settled() and unit.wait(0)


def test_a_bad_input_path_is_refused_not_materialised() -> None:
    box = FakeSandbox()
    got = _exec(box).run(["cat"], {"../escape": b"x"})
    assert got.code == "exec_invalid" and box.requests == []
