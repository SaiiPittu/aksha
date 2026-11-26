"""Environment images (ADR-P28): the key, pinned packages, approval, fetch, build, hit.

The backend is the recording fake and the network is a fake resolver and transport, so
nothing leaves the machine; the build box's offline install is the fake's rule.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

import pytest

from aksha.adapters.blobs.filesystem import FilesystemBlobStore
from aksha.runtime.canonical import sha256_ref
from aksha.runtime.egress.ports import HttpRequest, HttpResponse, Network
from aksha.runtime.egress.types import EgressPolicy
from aksha.runtime.sandbox.exec import ExecUnit
from aksha.runtime.sandbox.images import (
    EnvironmentImages,
    LockError,
    environment_key,
    pinned_packages,
    recorded_images,
)
from aksha.runtime.sandbox.types import (
    BoxRequest,
    BoxResult,
    EnvironmentMount,
    EnvironmentSpec,
    TreeManifest,
)
from tests.support.sandbox import FakeSandbox

WHEEL = b"SYN-wheel-bytes"
WHEEL_URL = "https://pkgs.example.org/files/syn_pkg-1.0-py3-none-any.whl"
LOCK = f"{WHEEL_URL} --hash={sha256_ref(WHEEL)}\n".encode()
SPEC = EnvironmentSpec("py", base="python@sha256:" + "b" * 64, lock_inputs=("requirements.lock",),
                       build_argv=("sh", "-c", "install"), egress="ext.index",
                       preapprove_pinned=True)  # fmt: skip
INDEX = EgressPolicy("ext.index", allow_hosts=("pkgs.example.org",))


class FakeResolver:
    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        return {"pkgs.example.org": ("93.184.215.14",)}.get(host, ())


class FakeTransport:
    def __init__(self, body: bytes = WHEEL) -> None:
        self.body = body
        self.sent: list[str] = []

    def send(
        self, request: HttpRequest, address: str, *, timeout_s: float, max_bytes: int
    ) -> HttpResponse:
        self.sent.append(request.url)
        self.max_bytes = max_bytes
        return HttpResponse(200, {}, self.body, request.url)


def install(request: BoxRequest, blobs: object) -> BoxResult:
    """The offline install: copies every package under out/env/pkgs and writes a tool."""
    collected = {f"out/env/pkgs/{p.split('/')[-1]}": b for p, b in request.files.items()
                 if p.startswith("pkgs/")}  # fmt: skip
    collected["out/env/bin/syn-tool"] = b"#!/bin/sh\necho tool\n"
    return BoxResult("ok", 0, b"installed", b"", collected=collected)


def _images(
    tmp_path: Path,
    box: FakeSandbox,
    *,
    spec: EnvironmentSpec = SPEC,
    transport: FakeTransport | None = None,
    recorded: Mapping[str, str] | None = None,
    approved: frozenset[str] = frozenset(),
) -> tuple[EnvironmentImages, FilesystemBlobStore]:
    blobs = FilesystemBlobStore(tmp_path / "blobs")
    network = Network(FakeResolver(), transport or FakeTransport())
    images = EnvironmentImages(
        {"ext.py": spec},
        {"ext.index": INDEX},
        network,
        box,
        box.probe(),
        blobs,
        recorded=lambda: dict(recorded or {}),
        approved=approved,
        unit=ExecUnit(),
    )
    return images, blobs


def test_the_key_hashes_base_argv_and_lock_blobs_only() -> None:
    locks = {"requirements.lock": sha256_ref(LOCK)}
    key = environment_key(SPEC, locks)
    assert key.startswith("sha256:") and key == environment_key(SPEC, dict(locks))
    assert key != environment_key(SPEC, {"requirements.lock": sha256_ref(b"other")})
    other = EnvironmentSpec(SPEC.name, SPEC.base, SPEC.lock_inputs, ("sh",), SPEC.egress)
    assert key != environment_key(other, locks)


def test_pinned_packages_come_from_hashed_lines_and_uv_locks() -> None:
    (pkg,) = pinned_packages({"requirements.lock": LOCK + b"# a comment\n"})
    assert (pkg.url, pkg.sha256, pkg.filename) == (WHEEL_URL, sha256_ref(WHEEL),
                                                   "syn_pkg-1.0-py3-none-any.whl")  # fmt: skip
    digest = hashlib.sha256(WHEEL).hexdigest()
    uv = (
        '[[package]]\nname = "syn-pkg"\nversion = "1.0"\n'
        f'wheels = [{{ url = "{WHEEL_URL}", hash = "sha256:{digest}" }}]\n'
    ).encode()
    assert [p.url for p in pinned_packages({"uv.lock": uv})] == [WHEEL_URL]


def test_an_unpinned_package_fails_the_build() -> None:
    with pytest.raises(LockError) as error:
        pinned_packages({"requirements.lock": f"{WHEEL_URL}\n".encode()})
    assert error.value.code == "package_unpinned"


def test_the_first_build_of_a_key_needs_approval(tmp_path: Path) -> None:
    box = FakeSandbox(rule=install)
    spec = EnvironmentSpec(SPEC.name, SPEC.base, SPEC.lock_inputs, SPEC.build_argv, SPEC.egress)
    transport = FakeTransport()
    images, _ = _images(tmp_path, box, spec=spec, transport=transport)
    got, noted = images.resolve("ext.py", None, {"requirements.lock": LOCK})
    assert got == "environment_approval_required" and noted["key"].startswith("sha256:")
    assert box.requests == [] and transport.sent == []


def test_a_preapproved_build_fetches_under_the_egress_policy_and_installs_offline(
    tmp_path: Path,
) -> None:
    box = FakeSandbox(rule=install)
    transport = FakeTransport()
    images, blobs = _images(tmp_path, box, transport=transport)
    got, noted = images.resolve("ext.py", None, {"requirements.lock": LOCK})
    assert isinstance(got, EnvironmentMount) and transport.sent == [WHEEL_URL]
    (build,) = box.requests
    assert build.policy.network == "none" and build.argv == SPEC.build_argv
    assert set(build.files) == {"requirements.lock", "pkgs/syn_pkg-1.0-py3-none-any.whl"}
    assert set(got.tree.entries) == {"pkgs/syn_pkg-1.0-py3-none-any.whl", "bin/syn-tool"}
    assert noted["built"] is True and noted["approval"] == "preapproved_pinned"
    assert noted["packages"] == [{"url": WHEEL_URL, "sha256": sha256_ref(WHEEL)}]
    assert [d["allowed"] for d in noted["egress"]] == [True]
    assert blobs.get(noted["manifest"]) and noted["image"] == got.tree.hash_ref()
    assert noted["build"]["status"] == "ok" and noted["build"]["exit_code"] == 0


def test_a_package_fetch_is_not_capped_at_the_page_body_limit(tmp_path: Path) -> None:
    """A wheel is often larger than a fetched page: the fetch has its own, larger cap."""
    transport = FakeTransport()
    images, _ = _images(tmp_path, FakeSandbox(rule=install), transport=transport)
    images.resolve("ext.py", None, {"requirements.lock": LOCK})
    assert transport.max_bytes >= 256 * 1024 * 1024


def test_a_package_whose_bytes_do_not_match_its_hash_fails_the_build(tmp_path: Path) -> None:
    box = FakeSandbox(rule=install)
    images, _ = _images(tmp_path, box, transport=FakeTransport(b"tampered"))
    got, _ = images.resolve("ext.py", None, {"requirements.lock": LOCK})
    assert got == "package_hash_mismatch" and box.requests == []


def test_a_package_outside_the_index_policy_is_refused_and_recorded(tmp_path: Path) -> None:
    evil = b"https://evil.example/x.whl --hash=" + sha256_ref(WHEEL).encode() + b"\n"
    box = FakeSandbox(rule=install)
    images, _ = _images(tmp_path, box)
    got, noted = images.resolve("ext.py", None, {"requirements.lock": evil})
    assert got == "egress_refused" and box.requests == []
    assert noted["egress"][0]["reason"] == "host_not_allowed"


def test_a_recorded_build_is_a_hit_with_no_fetch_and_no_box(tmp_path: Path) -> None:
    box = FakeSandbox(rule=install)
    first, blobs = _images(tmp_path, box)
    _, noted = first.resolve("ext.py", None, {"requirements.lock": LOCK})
    transport = FakeTransport()
    again = EnvironmentImages(
        {"ext.py": SPEC}, {"ext.index": INDEX}, Network(FakeResolver(), transport), box,
        box.probe(), blobs, recorded=lambda: {noted["key"]: noted["manifest"]},
    )  # fmt: skip
    got, hit = again.resolve("ext.py", None, {"requirements.lock": LOCK})
    assert isinstance(got, EnvironmentMount) and hit == {"key": noted["key"], "hit": True,
                                                         "manifest": noted["manifest"]}  # fmt: skip
    assert len(box.requests) == 1 and transport.sent == []


def test_an_explicit_grant_approves_one_key(tmp_path: Path) -> None:
    spec = EnvironmentSpec(SPEC.name, SPEC.base, SPEC.lock_inputs, SPEC.build_argv, SPEC.egress)
    box = FakeSandbox(rule=install)
    locks = {"requirements.lock": sha256_ref(LOCK)}
    images, _ = _images(
        tmp_path, box, spec=spec, approved=frozenset({environment_key(spec, locks)})
    )
    got, noted = images.resolve("ext.py", None, {"requirements.lock": LOCK})
    assert isinstance(got, EnvironmentMount) and noted["approval"] == "granted"


def test_the_lock_files_can_come_from_the_workspace_tree(tmp_path: Path) -> None:
    box = FakeSandbox(rule=install)
    images, blobs = _images(tmp_path, box)
    tree = TreeManifest({"requirements.lock": blobs.put(LOCK)})
    got, _ = images.resolve("ext.py", tree, {})
    assert isinstance(got, EnvironmentMount)
    missing, _ = images.resolve("ext.py", None, {})
    assert missing == "environment_lock_missing"


def test_an_unknown_environment_is_refused(tmp_path: Path) -> None:
    images, _ = _images(tmp_path, FakeSandbox())
    assert images.resolve("ext.nope", None, {})[0] == "environment_unknown"


def test_recorded_images_read_builds_from_exec_records() -> None:
    value = {"exec": [{"environment": {"key": "sha256:k", "built": True,
                                       "manifest": "sha256:m"}}]}  # fmt: skip
    assert recorded_images([value]) == {"sha256:k": "sha256:m"}
