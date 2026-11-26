"""Sandbox values (ADR-P12, P28): backends, environments, policies, requests and results.

``Strength`` orders backends; ``Availability`` is one probe's answer, which the policy gate
reads to refuse an exec tool that would run unconfined (ADR-P11 layer 7). ``SandboxPolicy``
says what one exec may touch; ``narrow`` intersects two layers and raises ``PolicyWidened``
when the narrower one asks for more (decision 2). ``Limits`` are the hard limits, and
``ExecPolicy`` joins the narrowed policy and the limits into what one box runs under.
``SandboxNeeds`` is an exec tool's declaration. ``TreeManifest`` names a workspace tree
by path and content hash (decision 5). ``BoxRequest`` and ``BoxResult`` are the whole of
what crosses the ``Sandbox`` port (T3): argv, inputs, the effective policy and limits in;
exit status, output, denials and collected files out. ``SessionRef`` names the session
whose one stateful workspace a box runs in (decision 37).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any, Literal

from aksha.runtime.canonical import sha256_hex
from aksha.runtime.sandbox.errors import PolicyWidened

Strength = Literal["none", "os-jail", "container", "vm"]
"""Isolation strengths, weakest first."""

STRENGTHS: tuple[Strength, ...] = ("none", "os-jail", "container", "vm")
"""The strengths in order, weakest first."""


def strong_enough(have: Strength, need: Strength) -> bool:
    """Whether a backend of strength ``have`` meets a tool's ``need``."""
    return STRENGTHS.index(have) >= STRENGTHS.index(need)


@dataclass(frozen=True)
class Availability:
    """What probing one sandbox backend found: its strength and whether it can run now."""

    backend: str
    strength: Strength
    available: bool
    reason: str = ""


@dataclass(frozen=True)
class SandboxBackendSpec:
    """A sandbox backend an extension contributes: a name and a factory for the backend.

    The factory's types are fixed when the ``SandboxBackend`` port is built.
    """

    name: str
    factory: Callable[..., object]


@dataclass(frozen=True)
class EnvironmentSpec:
    """An execution environment image: a pinned base, the inputs that key it, how to build it.

    ``egress`` names the egress policy the runtime fetches packages under (ADR-P28 D3).
    ``preapprove_pinned`` is the workspace strategy's WS-14 lean: a first build needs no
    person when every package is pinned by hash and fetched under that policy.
    """

    name: str
    base: str
    lock_inputs: tuple[str, ...]
    build_argv: tuple[str, ...]
    egress: str
    preapprove_pinned: bool = False


@dataclass(frozen=True)
class Limits:
    """Hard limits on one exec (decision 7; the sandboxing note's 30 s, 512 MB, 64 processes).

    ``max_output_bytes`` caps stdout and stderr each; ``max_file_bytes`` is the largest file
    the program may write; ``max_collect_bytes`` caps everything collected back.
    """

    wall_ms: int = 30_000
    cpu_s: int = 30
    memory_mb: int = 512
    max_processes: int = 64
    max_open_files: int = 256
    max_output_bytes: int = 1_048_576
    max_file_bytes: int = 16 * 1_048_576
    max_collect_bytes: int = 64 * 1_048_576

    def narrow(self, other: Limits) -> Limits:
        """The smaller of each limit."""
        return Limits(**{f.name: min(getattr(self, f.name), getattr(other, f.name))
                         for f in fields(self)})  # fmt: skip

    def as_json(self) -> dict[str, int]:
        """The limits as recorded data."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _under(path: str, parent: str) -> bool:
    """Whether relative ``path`` is ``parent`` or inside it (``.`` is the whole tree)."""
    return parent == "." or path == parent or path.startswith(parent.rstrip("/") + "/")


def check_relative(path: str) -> str:
    """``path`` if it is a clean relative path inside ``/work``; ``ValueError`` otherwise."""
    if path == ".":
        return path
    parts = path.split("/")
    if not path or path.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"{path!r} is not a clean relative path inside /work")
    if "\x00" in path:
        raise ValueError("a path holds NUL")
    return path


@dataclass(frozen=True)
class SandboxPolicy:
    """What one exec may touch: writable and protected paths under ``/work``, and the network.

    ``protected`` is empty by default: which folders an agent's tools must never write is the
    agent's (or a sandbox extension's) declaration, never a runtime assumption. ``.`` in
    ``writable`` makes the whole tree writable (a writable workspace tree). Limits are
    declared beside the policy (``SandboxNeeds.limits``) and joined to it in ``ExecPolicy``.
    """

    writable: tuple[str, ...] = ("out", "tmp")
    protected: tuple[str, ...] = ()
    host_read: tuple[str, ...] = ()
    network: Literal["none", "allowlist"] = "none"

    def __post_init__(self) -> None:
        """Every path is a clean relative path inside ``/work``."""
        for path in (*self.writable, *self.protected):
            check_relative(path)

    def narrow(self, other: SandboxPolicy) -> SandboxPolicy:
        """This policy restricted by ``other``; raises ``PolicyWidened`` if ``other`` widens.

        ``other``'s writable paths must each lie under one of this policy's; protected paths
        add up; host reads must be a subset; the network can go from ``allowlist`` to
        ``none``, never back.
        """
        for path in other.writable:
            if not any(_under(path, mine) for mine in self.writable):
                raise PolicyWidened(message=f"writable {path!r} is outside {self.writable}")
        extra = set(other.host_read) - set(self.host_read)
        if extra:
            raise PolicyWidened(message=f"host reads {sorted(extra)} are not allowed")
        if other.network != "none" and self.network == "none":
            raise PolicyWidened(message="the network is off above this layer")
        protected = tuple(dict.fromkeys((*self.protected, *other.protected)))
        return SandboxPolicy(other.writable, protected, other.host_read, other.network)

    def as_json(self) -> dict[str, Any]:
        """The policy as recorded data."""
        return {
            "writable": list(self.writable),
            "protected": list(self.protected),
            "host_read": list(self.host_read),
            "network": self.network,
        }


@dataclass(frozen=True)
class ExecPolicy:
    """The effective policy of one exec: the narrowed ``SandboxPolicy`` and the limits."""

    paths: SandboxPolicy = SandboxPolicy()
    limits: Limits = Limits()

    @classmethod
    def of(
        cls,
        *,
        writable: tuple[str, ...] = ("out", "tmp"),
        protected: tuple[str, ...] = (),
        network: Literal["none", "allowlist"] = "none",
        limits: Limits | None = None,
    ) -> ExecPolicy:
        """An exec policy from its parts."""
        paths = SandboxPolicy(writable, protected, (), network)
        return cls(paths, limits or Limits())

    @property
    def writable(self) -> tuple[str, ...]:
        """The writable paths."""
        return self.paths.writable

    @property
    def protected(self) -> tuple[str, ...]:
        """The protected paths."""
        return self.paths.protected

    @property
    def network(self) -> Literal["none", "allowlist"]:
        """The network setting."""
        return self.paths.network

    def as_json(self) -> dict[str, Any]:
        """The policy and limits as recorded data."""
        return {**self.paths.as_json(), "limits": self.limits.as_json()}

    def hash_ref(self) -> str:
        """``sha256:`` over the canonical JSON of :meth:`as_json`."""
        return "sha256:" + sha256_hex(self.as_json())


@dataclass(frozen=True)
class TreeManifest:
    """A workspace tree: each relative path to the ``sha256:`` ref of its blob (decision 5).

    Flat for now; ADR-P12 S2's nested tree objects are an amendment the build deferred.
    """

    entries: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        """Every path is clean and relative; the mapping is frozen."""
        for path in self.entries:
            if path == ".":
                raise ValueError("'.' is not a file path")
            check_relative(path)
        object.__setattr__(self, "entries", MappingProxyType(dict(sorted(self.entries.items()))))

    def as_json(self) -> dict[str, str]:
        """The manifest as recorded data."""
        return dict(self.entries)

    def hash_ref(self) -> str:
        """``sha256:`` over the canonical JSON of the entries."""
        return "sha256:" + sha256_hex(self.as_json())


@dataclass(frozen=True)
class SandboxNeeds:
    """What an ``exec`` tool needs from the sandbox (``ToolSpec.sandbox``).

    ``tree_input`` names the argument holding a workspace tree ref; ``environment`` names
    an ``EnvironmentSpec`` mounted read-only (ADR-P12 S1).
    """

    min_strength: Strength = "os-jail"
    policy: SandboxPolicy = SandboxPolicy()
    tree_input: str | None = None
    tree_writable: bool = False
    inputs: tuple[str, ...] = ()
    environment: str | None = None
    outputs: tuple[str, ...] = ()
    """Declared output paths under ``/work`` (decision 8); empty collects all of ``out``."""
    limits: Limits = Limits()
    """The limits the tool needs; the run ceiling's limits cap them (decision 7)."""


DenialKind = Literal["write", "connect", "process", "limit", "symlink", "output"]


@dataclass(frozen=True)
class Denial:
    """One blocked action, returned as data the model reads (decision 8)."""

    kind: DenialKind
    detail: str

    def as_json(self) -> dict[str, str]:
        """The denial as recorded data."""
        return {"kind": self.kind, "detail": self.detail}


@dataclass(frozen=True)
class EnvironmentMount:
    """An environment image mounted read-only at ``/env``: its key and its file tree."""

    key: str
    tree: TreeManifest


@dataclass(frozen=True)
class SessionRef:
    """The session a box belongs to: one stateful workspace per owner and session (decision 37).

    Every exec of the session's runs names it, so a backend that keeps a workspace per
    session (a folder bound as ``/work``, a remote sandbox kept by its ID) finds the same
    one each time.
    """

    owner: str
    session: str

    def __post_init__(self) -> None:
        """Both parts are non-empty and hold no NUL."""
        for part in (self.owner, self.session):
            if not part or "\x00" in part:
                raise ValueError("a session reference names a non-empty owner and session")

    def as_json(self) -> dict[str, str]:
        """The reference as recorded data."""
        return {"owner": self.owner, "session": self.session}


Collect = Literal["all", "changed"]
"""What a session box collects back: every file under the writable paths, or only the
files this exec wrote or changed (decision 37). A fresh box always collects everything."""


@dataclass(frozen=True)
class BoxRequest:
    """Everything the runtime sends a backend for one box (T3): data only, no handles.

    ``files`` are small inputs given inline; ``tree`` is a workspace tree whose blobs the
    backend reads through the reader it is given (the runtime streams them, T4's lean).
    ``session`` names the stateful workspace the box runs in (decision 37): the inputs are
    written over it and nothing else is removed. None is a fresh ``/work`` that the backend
    deletes afterwards. ``collect`` applies to a session box only.
    """

    invocation_id: str
    argv: tuple[str, ...]
    policy: ExecPolicy
    files: Mapping[str, bytes] = field(default_factory=lambda: MappingProxyType({}))
    tree: TreeManifest | None = None
    environment: EnvironmentMount | None = None
    stdin: bytes = b""
    session: SessionRef | None = None
    collect: Collect = "changed"

    def __post_init__(self) -> None:
        """An argv is a non-empty tuple of strings; every file path is clean."""
        if not self.argv or not all(isinstance(a, str) for a in self.argv):
            raise ValueError("argv is a non-empty sequence of strings")
        for path in self.files:
            check_relative(path)


ExecStatus = Literal["ok", "exec_failed", "exec_timeout", "exec_limit", "output_rejected"]


@dataclass(frozen=True)
class BoxResult:
    """What a backend returns for one box (T3): status, output, denials, collected files.

    ``collected`` holds every regular file under a writable path, by path under ``/work``,
    read symlink-safe and capped by ``max_collect_bytes`` (``output_rejected`` past it).
    """

    status: ExecStatus
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    denials: tuple[Denial, ...] = ()
    collected: Mapping[str, bytes] = field(default_factory=lambda: MappingProxyType({}))
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    workspace: Mapping[str, Any] | None = None
    """For a session box, which workspace it ran in (decision 37), as recorded data: the
    backend, the session folder or remote sandbox ID, where it came from, and any
    checkpoint made. None for a fresh box."""
