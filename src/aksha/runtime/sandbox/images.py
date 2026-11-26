"""Execution environment images (ADR-P28): built once per key, recorded, mounted at ``/env``.

:func:`environment_key` is the SHA-256 over the base, the build argv and the blob refs of
the declared lock files, so a lock-file edit changes the key and an unrelated edit does not
(D2). :meth:`EnvironmentImages.resolve` answers one exec's environment, in order:

1. The lock files come from the exec's inline files, else its workspace tree
   (``environment_lock_missing`` otherwise); the key follows.
2. A key with a recorded build is a hit (D5): its manifest blob is read, nothing is
   fetched or built, and the read is recorded (``hit``).
3. Otherwise the first build needs approval (D4): an explicit grant of the key, or the
   spec's WS-14 pre-approval when every package is pinned by hash. Neither answers
   ``environment_approval_required`` and nothing is fetched or run.
4. The runtime fetches (D3): each pinned package through the policed HTTP client bound to
   the spec's egress policy, checked against its lock hash (``package_hash_mismatch``)
   and stored as a blob. A refused destination answers ``egress_refused``.
5. The box installs offline (D3): the lock files and ``pkgs/<file>`` are its only inputs,
   the network is off, and everything it writes under ``out/env`` is the image. The image
   files go to the blob store with a manifest blob, and the build is recorded: key,
   packages, egress decisions, the build box's record, the image hash and the manifest.

The record rides on the exec record of the step that needed the image, so the build is
in the event store; :func:`recorded_images` reads those records back as the cache index.
The blob store holds the image, and any cache below it is a backend detail.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.canonical import canonical_json, sha256_hex, sha256_ref
from aksha.runtime.egress.client import PolicedHttpClient
from aksha.runtime.egress.errors import EGRESS_REFUSED, EgressRefused
from aksha.runtime.egress.ports import Network
from aksha.runtime.egress.types import EgressPolicy
from aksha.runtime.errors import AkshaError
from aksha.runtime.sandbox.errors import SandboxRefused
from aksha.runtime.sandbox.exec import ExecUnit
from aksha.runtime.sandbox.ports import Sandbox
from aksha.runtime.sandbox.types import (
    Availability,
    BoxRequest,
    EnvironmentMount,
    EnvironmentSpec,
    ExecPolicy,
    TreeManifest,
)

IMAGE_PREFIX = "out/env/"
FETCH_TIMEOUT_S = 60.0
PACKAGE_MAX_BYTES = 512 * 1024 * 1024
"""The body cap of one package fetch: wheels are often larger than the 5 MiB page cap."""
BUILD_POLICY = ExecPolicy.of(writable=("out", "tmp"))


class LockError(AkshaError):
    """A lock file the build cannot use: ``package_unpinned``, ``lock_unreadable``."""

    default_code = "lock_unreadable"


@dataclass(frozen=True)
class PinnedPackage:
    """One package file a lock pins: where to fetch it and the hash it must have."""

    url: str
    sha256: str

    @property
    def filename(self) -> str:
        """The last path segment of the URL."""
        return urlsplit(self.url).path.rsplit("/", 1)[-1] or "package"


def environment_key(spec: EnvironmentSpec, locks: Mapping[str, str]) -> str:
    """``sha256:`` over the base, the build argv and each lock file's blob ref. Pure."""
    body = {"base": spec.base, "build_argv": list(spec.build_argv), "locks": dict(locks)}
    return "sha256:" + sha256_hex(body)


def _hashed(text: str) -> str:
    text = text.strip()
    if text.startswith("sha256:") and len(text) == 71:
        return text
    raise LockError("package_unpinned", f"{text!r} is not a sha256 hash")


def pinned_packages(locks: Mapping[str, bytes]) -> tuple[PinnedPackage, ...]:
    """Every package the lock files pin, in order (``uv.lock``, or hashed URL lines).

    A ``*.lock``/``*.txt`` file holds one ``<url> --hash=sha256:<hex>`` per line (``#``
    comments allowed); a ``uv.lock`` lists ``wheels`` and ``sdist`` entries with ``url``
    and ``hash``. Any other declared file (``pyproject.toml``) keys the image but pins no
    package. A URL with no hash refuses the build (``package_unpinned``).
    """
    out: dict[str, PinnedPackage] = {}
    for path, data in locks.items():
        name = path.rsplit("/", 1)[-1]
        if name == "uv.lock":
            try:
                doc = tomllib.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
                raise LockError("lock_unreadable", f"{path}: {exc}") from exc
            for package in doc.get("package", []):
                files = [*package.get("wheels", []), package.get("sdist") or {}]
                for entry in files:
                    if "url" in entry:
                        pkg = PinnedPackage(str(entry["url"]), _hashed(str(entry.get("hash", ""))))
                        out.setdefault(pkg.url, pkg)
        elif name.endswith((".lock", ".txt")):
            for raw in data.decode("utf-8", "replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                url, _, rest = line.partition(" ")
                if "--hash=" not in rest:
                    raise LockError("package_unpinned", f"{path}: {url} has no --hash")
                pkg = PinnedPackage(url, _hashed(rest.split("--hash=", 1)[1].split()[0]))
                out.setdefault(pkg.url, pkg)
    return tuple(out.values())


def recorded_images(values: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Each built key's manifest ref, read from step values' exec records. Pure."""
    out: dict[str, str] = {}
    for value in values:
        for record in value.get("exec", ()) or ():
            env = record.get("environment") or {}
            if env.get("built") is True and env.get("key") and env.get("manifest"):
                out[str(env["key"])] = str(env["manifest"])
    return out


class EnvironmentImages:
    """Resolves an exec's environment to a mounted image (module doc)."""

    def __init__(
        self,
        specs: Mapping[str, EnvironmentSpec],
        egress: Mapping[str, EgressPolicy],
        network: Network | None,
        box: Sandbox | None,
        availability: Availability | None,
        blobs: BlobStore | None,
        *,
        recorded: Callable[[], Mapping[str, str]] = dict,
        approved: frozenset[str] = frozenset(),
        unit: ExecUnit | None = None,
        build_policy: ExecPolicy = BUILD_POLICY,
    ) -> None:
        """Specs and egress policies by full name; ``recorded`` is the built-key index."""
        self._specs = specs
        self._egress = egress
        self._network = network
        self._box = box
        self._availability = availability
        self._blobs = blobs
        self._recorded = recorded
        self._approved = approved
        self._unit = unit or ExecUnit()
        self._build_policy = build_policy

    def resolve(
        self, name: str, tree: TreeManifest | None, files: Mapping[str, bytes]
    ) -> tuple[EnvironmentMount | str, dict[str, Any]]:
        """The mount for ``name``, or why not, and what to record (module doc)."""
        spec = self._specs.get(name)
        if spec is None:
            return "environment_unknown", {}
        blobs = self._blobs
        if blobs is None:
            return "environment_unavailable", {}
        locks: dict[str, bytes] = {}
        for path in spec.lock_inputs:
            if path in files:
                locks[path] = files[path]
            elif tree is not None and path in tree.entries:
                locks[path] = blobs.get(tree.entries[path])
            else:
                return "environment_lock_missing", {"missing": path}
        key = environment_key(spec, {p: sha256_ref(b) for p, b in locks.items()})
        noted: dict[str, Any] = {"key": key}
        manifest = self._recorded().get(key)
        if manifest is not None:
            return EnvironmentMount(key, _manifest(blobs.get(manifest))), {
                **noted, "hit": True, "manifest": manifest,
            }  # fmt: skip
        try:
            packages = pinned_packages(locks)
        except LockError as exc:
            return exc.code, noted
        if key in self._approved:
            noted["approval"] = "granted"
        elif spec.preapprove_pinned:
            noted["approval"] = "preapproved_pinned"
        else:
            return "environment_approval_required", noted
        noted["packages"] = [{"url": p.url, "sha256": p.sha256} for p in packages]
        fetched = self._fetch(spec, packages, noted)
        if isinstance(fetched, str):
            return fetched, noted
        return self._build(spec, key, locks, fetched, noted)

    def _fetch(
        self, spec: EnvironmentSpec, packages: tuple[PinnedPackage, ...], noted: dict[str, Any]
    ) -> dict[str, bytes] | str:
        if not packages:
            return {}
        policy = self._egress.get(spec.egress)
        if policy is None:
            return "egress_policy_unknown"
        if self._network is None:
            noted["egress"] = []
            return "network_off"
        client = PolicedHttpClient(policy, self._network.resolver, self._network.transport,
                                   max_bytes=PACKAGE_MAX_BYTES)  # fmt: skip
        out: dict[str, bytes] = {}
        try:
            for pkg in packages:
                body = client.get(pkg.url, timeout_s=FETCH_TIMEOUT_S)
                if body.status != 200 or body.truncated:
                    return "package_fetch_failed"
                if sha256_ref(body.body) != pkg.sha256:
                    return "package_hash_mismatch"
                out[f"pkgs/{pkg.filename}"] = body.body
        except EgressRefused:
            return EGRESS_REFUSED
        finally:
            noted["egress"] = [d.as_json() for d in client.decisions]
        return out

    def _build(
        self,
        spec: EnvironmentSpec,
        key: str,
        locks: Mapping[str, bytes],
        packages: Mapping[str, bytes],
        noted: dict[str, Any],
    ) -> tuple[EnvironmentMount | str, dict[str, Any]]:
        box, probe, blobs = self._box, self._availability, self._blobs
        assert blobs is not None
        if box is None or probe is None or not probe.available:
            return "sandbox_unavailable", noted
        request = BoxRequest(f"env:{key}", spec.build_argv, self._build_policy,
                             files={**locks, **packages})  # fmt: skip
        try:
            result = box.run(request, blobs, self._unit)
        except SandboxRefused as exc:
            return exc.code, noted
        noted["build"] = {
            "backend": box.name, "version": box.version(), "status": result.status,
            "exit_code": result.exit_code, "policy_hash": self._build_policy.hash_ref(),
            "denials": [d.as_json() for d in result.denials],
        }  # fmt: skip
        if result.status != "ok":
            return "environment_build_failed", noted
        entries = {
            path[len(IMAGE_PREFIX) :]: blobs.put(data)
            for path, data in result.collected.items()
            if path.startswith(IMAGE_PREFIX)
        }
        tree = TreeManifest(entries)
        manifest = blobs.put(canonical_json(tree.as_json()).encode("utf-8"))
        noted.update({"built": True, "image": tree.hash_ref(), "manifest": manifest})
        return EnvironmentMount(key, tree), noted


def _manifest(data: bytes) -> TreeManifest:
    return TreeManifest(json.loads(data.decode("utf-8")))
