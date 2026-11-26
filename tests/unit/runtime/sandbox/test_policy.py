"""Sandbox policy (ADR-P12 decisions 2, 3, 4, 7): limits, narrowing, the effective policy."""

from __future__ import annotations

import pytest

from aksha.runtime.sandbox.errors import PolicyWidened
from aksha.runtime.sandbox.policy import effective_policy
from aksha.runtime.sandbox.types import (
    ExecPolicy,
    Limits,
    SandboxNeeds,
    SandboxPolicy,
    TreeManifest,
)


def test_the_default_limits_are_the_sandboxing_note_values() -> None:
    limits = Limits()
    assert (limits.wall_ms, limits.memory_mb, limits.max_processes) == (30_000, 512, 64)
    assert ExecPolicy().limits == limits and SandboxNeeds().limits == limits


def test_limits_narrow_to_the_smaller_value_field_by_field() -> None:
    got = Limits(wall_ms=30_000, memory_mb=512).narrow(Limits(wall_ms=5_000, memory_mb=1_024))
    assert (got.wall_ms, got.memory_mb) == (5_000, 512)


def test_narrowing_keeps_only_writable_paths_the_ceiling_allows() -> None:
    ceiling = SandboxPolicy(writable=("out", "tmp"))
    assert ceiling.narrow(SandboxPolicy(writable=("out",))).writable == ("out",)
    assert ceiling.narrow(SandboxPolicy(writable=("out/reports",))).writable == ("out/reports",)
    with pytest.raises(PolicyWidened) as widened:
        ceiling.narrow(SandboxPolicy(writable=("src",)))
    assert widened.value.code == "policy_widened"


def test_a_whole_tree_writable_ceiling_admits_any_relative_path() -> None:
    whole = SandboxPolicy(writable=(".",))
    assert whole.narrow(SandboxPolicy(writable=("src", "out"))).writable == ("src", "out")


def test_protected_paths_add_up_and_never_drop() -> None:
    a = SandboxPolicy(writable=(".",), protected=(".git",))
    b = SandboxPolicy(writable=(".",), protected=("secrets",))
    assert a.narrow(b).protected == (".git", "secrets")


def test_the_network_and_host_reads_can_only_narrow() -> None:
    with pytest.raises(PolicyWidened):
        SandboxPolicy().narrow(SandboxPolicy(network="allowlist"))
    with pytest.raises(PolicyWidened):
        SandboxPolicy().narrow(SandboxPolicy(host_read=("/home",)))


def test_the_hash_names_the_policy_and_its_limits() -> None:
    a, b = ExecPolicy(), ExecPolicy.of(writable=("out",))
    c = ExecPolicy.of(limits=Limits(wall_ms=1))
    assert a.hash_ref() == ExecPolicy().hash_ref() and a.hash_ref().startswith("sha256:")
    assert len({a.hash_ref(), b.hash_ref(), c.hash_ref()}) == 3


def test_the_effective_policy_intersects_every_layer_with_the_tool_needs() -> None:
    ceiling = ExecPolicy.of(writable=(".",), limits=Limits(wall_ms=60_000))
    task = SandboxPolicy(writable=("out", "src"))
    needs = SandboxNeeds(policy=SandboxPolicy(writable=("out",), protected=(".git",)),
                         limits=Limits(wall_ms=20_000))  # fmt: skip
    got = effective_policy(ceiling, None, task, needs)
    assert got.writable == ("out",) and got.protected == (".git",)
    assert got.limits.wall_ms == 20_000
    loose = SandboxNeeds(limits=Limits(wall_ms=90_000))
    assert effective_policy(ceiling, None, None, loose).limits.wall_ms == 60_000


def test_a_writable_tree_needs_the_whole_tree_writable() -> None:
    needs = SandboxNeeds(tree_writable=True, policy=SandboxPolicy(writable=(".",)))
    whole = ExecPolicy.of(writable=(".",))
    assert effective_policy(whole, None, None, needs).writable == (".",)
    with pytest.raises(PolicyWidened):
        effective_policy(ExecPolicy(), None, None, needs)


def test_a_tree_manifest_refuses_paths_that_leave_the_work_folder() -> None:
    ok = TreeManifest({"src/a.py": "sha256:" + "0" * 64})
    assert ok.hash_ref().startswith("sha256:")
    for bad in ("/etc/passwd", "../x", "a/../../x", "", "a//b"):
        with pytest.raises(ValueError):
            TreeManifest({bad: "sha256:" + "0" * 64})
