"""``BoxedExec``: the ``ExecAPI`` an exec tool body gets, one per call attempt (ADR-P12).

Each :meth:`BoxedExec.run` is one box. Given the run's session (decision 37), the box runs
in that session's one stateful workspace: the workspace tree and the inline files are
written over it, the program runs under the effective policy, and what it wrote stays for
the session's next exec; the outputs are the files this exec wrote or changed (all of them
for a writable tree). Given no session, ``/work`` is built fresh from the tree and the
files and the box keeps nothing (decision 5). In order:

1. No backend, a backend that did not probe available, or one weaker than the tool's
   ``min_strength`` answers ``sandbox_unavailable`` and nothing spawns (decision 2).
2. A policy with the network on answers ``network_unsupported``: every shipped backend
   refuses ``allowlist`` (decision 4).
3. A tool that names an environment gets it mounted at ``/env`` (ADR-P28), or the reason
   it cannot (``environment_unknown``, ``environment_approval_required``, ...).
4. The backend runs the box and returns its result; outputs come back as data (decision
   8): the declared outputs (all of ``out`` by default), and for a writable tree a new
   ``TreeManifest`` whose blobs are written to the blob store on the runtime thread.
5. Every run, refused or not, appends an exec record (decision 9) the agent loop puts on
   the step's ``step.completed`` under ``value["exec"]``. A session box's record names
   the session and the workspace the backend says it used (``workspace``).

``ExecUnit`` is the step's kill boundary (T6, ADR-P18 D6): the backend attaches the
running box to it, and the step executor ends it on a stop or at the deadline. A unit
asked to settle refuses every later run of the attempt.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.cancellation.ports import KillableUnit
from aksha.runtime.canonical import sha256_ref
from aksha.runtime.sandbox.errors import SANDBOX_UNAVAILABLE, SandboxRefused
from aksha.runtime.sandbox.ports import Sandbox
from aksha.runtime.sandbox.types import (
    Availability,
    BoxRequest,
    BoxResult,
    Denial,
    EnvironmentMount,
    ExecPolicy,
    SandboxNeeds,
    SessionRef,
    TreeManifest,
    strong_enough,
)

NETWORK_UNSUPPORTED = "network_unsupported"
STOPPED = "run_stopped"
BACKEND_ERROR = "backend_error"
TREE_UNREADABLE = "tree_unreadable"
DEFAULT_OUTPUT = "out"

EnvironmentResolver = Callable[
    [str, "TreeManifest | None", Mapping[str, bytes]],
    "tuple[EnvironmentMount | str, Mapping[str, Any]]",
]
"""An environment name, with the exec's tree and files (which hold the lock files), to its
mount or the code of why it cannot be mounted, and what to record (ADR-P28 D2, D5)."""


@dataclass(frozen=True)
class ExecOutcome:
    """What one exec did, as data the body (and through it the model) reads.

    ``status`` is a ``BoxResult`` status, or ``refused`` with ``code`` saying why nothing
    ran. ``outputs`` are the declared outputs by path; ``tree`` is the new workspace tree
    version when the tool's tree is writable (the body stages its save, ADR-P12 S4).
    """

    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    denials: tuple[Denial, ...] = ()
    outputs: Mapping[str, bytes] = field(default_factory=lambda: MappingProxyType({}))
    tree: TreeManifest | None = None
    code: str | None = None
    record: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def ok(self) -> bool:
        """Whether the program ran and exited 0."""
        return self.status == "ok"


class ExecUnit:
    """The kill boundary of one exec step: whichever box is running now (module doc)."""

    def __init__(self) -> None:
        """No box yet."""
        self._lock = threading.Lock()
        self._current: KillableUnit | None = None
        self._stopping = False

    @property
    def stopping(self) -> bool:
        """Whether a settle was requested; later runs are refused."""
        return self._stopping

    def attach(self, unit: KillableUnit) -> None:
        """A box started; a settle already requested ends it at once."""
        with self._lock:
            self._current = unit
            stopping = self._stopping
        if stopping:
            unit.terminate()

    def detach(self) -> None:
        """The box ended."""
        with self._lock:
            self._current = None

    def request_settle(self) -> None:
        """Ask the running box to stop, and refuse the rest of the attempt."""
        with self._lock:
            self._stopping = True
            current = self._current
        if current is not None:
            current.request_settle()

    def terminate(self) -> None:
        """End the running box now."""
        with self._lock:
            self._stopping = True
            current = self._current
        if current is not None:
            current.terminate()

    def settled(self) -> bool:
        """Whether no box is running."""
        with self._lock:
            current = self._current
        return current is None or current.settled()

    def wait(self, seconds: float) -> bool:
        """Wait up to ``seconds`` for the running box, if any."""
        with self._lock:
            current = self._current
        return True if current is None else current.wait(seconds)


class _NoBlobs:
    def get(self, ref: str) -> bytes:
        raise SandboxRefused(TREE_UNREADABLE, "this loop was given no blob store")


class BoxedExec:
    """The ``ExecAPI`` of one call attempt (module doc)."""

    def __init__(
        self,
        box: Sandbox | None,
        availability: Availability | None,
        needs: SandboxNeeds,
        policy: ExecPolicy,
        *,
        invocation: str,
        blobs: BlobStore | None = None,
        environments: EnvironmentResolver | None = None,
        unit: ExecUnit | None = None,
        refusal: tuple[str, str] | None = None,
        session: SessionRef | None = None,
    ) -> None:
        """Run under ``policy`` (already effective) on ``box``, recording every run.

        ``refusal`` refuses every run with that code and message (a policy that widened).
        ``session`` is the run's session, whose stateful workspace every box runs in.
        """
        self._box = box
        self._availability = availability
        self._needs = needs
        self._policy = policy
        self._invocation = invocation
        self._blobs = blobs
        self._environments = environments
        self.unit = unit or ExecUnit()
        self._refusal_given = refusal
        self._session = session
        self._records: list[dict[str, Any]] = []

    @property
    def records(self) -> tuple[Mapping[str, Any], ...]:
        """Every run's exec record, in order."""
        return tuple(self._records)

    def run(
        self,
        argv: Sequence[str],
        files: Mapping[str, bytes] | None = None,
        *,
        tree: TreeManifest | None = None,
        stdin: bytes = b"",
    ) -> ExecOutcome:
        """Run ``argv`` in one fresh box (module doc); never raises."""
        n = len(self._records) + 1
        base: dict[str, Any] = {
            "invocation": f"{self._invocation}/x{n}",
            "argv": list(argv),
            "policy_hash": self._policy.hash_ref(),
            "inputs": {p: sha256_ref(b) for p, b in sorted((files or {}).items())},
        }
        if tree is not None:
            base["tree"] = tree.hash_ref()
        if self._session is not None:
            base["session"] = self._session.as_json()
        refused = self._refusal()
        if refused is not None:
            return self._refused(base, *refused)
        box = self._box
        assert box is not None  # _refusal checked it
        base.update({"backend": box.name, "strength": box.strength, "version": box.version()})
        mount: EnvironmentMount | None = None
        name = self._needs.environment
        if name is not None:
            got: EnvironmentMount | str = "environment_unavailable"
            noted: Mapping[str, Any] = {}
            if self._environments is not None:
                got, noted = self._environments(name, tree, dict(files or {}))
            base["environment"] = {"name": name, **noted}
            if isinstance(got, str):
                return self._refused(base, got, f"environment {name!r}: {got}")
            mount = got
            base["environment"]["key"] = got.key
        try:
            request = BoxRequest(base["invocation"], tuple(argv), self._policy,
                                 files=dict(files or {}), tree=tree, environment=mount,
                                 stdin=stdin, session=self._session,
                                 collect="all" if self._needs.tree_writable else "changed",
                                 )  # fmt: skip
        except ValueError as exc:
            return self._refused(base, "exec_invalid", str(exc))
        try:
            result = box.run(request, self._blobs or _NoBlobs(), self.unit)
        except SandboxRefused as exc:
            return self._refused(base, exc.code, exc.message)
        except Exception as exc:  # a backend bug is a recorded refusal, never a crash
            return self._refused(base, BACKEND_ERROR, f"{type(exc).__name__}: {exc}")
        return self._finished(base, result, tree)

    def _refusal(self) -> tuple[str, str] | None:
        need = self._needs.min_strength
        box, probe = self._box, self._availability
        if self._refusal_given is not None:
            return self._refusal_given
        if self.unit.stopping:
            return STOPPED, "the step was asked to stop"
        if box is None or probe is None or not probe.available:
            why = "no sandbox backend" if probe is None else (probe.reason or "unavailable")
            return SANDBOX_UNAVAILABLE, f"needs a {need} sandbox; {why}"
        if not strong_enough(box.strength, need):
            return SANDBOX_UNAVAILABLE, f"needs a {need} sandbox; {box.name} is {box.strength}"
        if self._policy.network != "none":
            return NETWORK_UNSUPPORTED, "no shipped backend runs a box with the network on"
        return None

    def _refused(self, base: dict[str, Any], code: str, message: str) -> ExecOutcome:
        record = {**base, "status": "refused", "code": code}
        self._records.append(record)
        return ExecOutcome("refused", None, "", message, code=code, record=record)

    def _finished(
        self, base: dict[str, Any], result: BoxResult, tree: TreeManifest | None
    ) -> ExecOutcome:
        declared = self._needs.outputs or (DEFAULT_OUTPUT,)
        outputs = {
            path: data
            for path, data in result.collected.items()
            if any(path == d or path.startswith(d.rstrip("/") + "/") for d in declared)
        }
        new_tree: TreeManifest | None = None
        if self._needs.tree_writable and result.status == "ok":
            new_tree = self._collect_tree(result.collected, tree)
        record: dict[str, Any] = {
            **base,
            "status": result.status,
            "exit_code": result.exit_code,
            "denials": [d.as_json() for d in result.denials],
            "outputs": {p: sha256_ref(b) for p, b in sorted(outputs.items())},
        }
        if new_tree is not None:
            record["new_tree"] = new_tree.hash_ref()
        if result.workspace is not None:
            record["workspace"] = dict(result.workspace)
        if result.stdout_truncated or result.stderr_truncated:
            record["truncated"] = True
        self._records.append(record)
        return ExecOutcome(
            result.status,
            result.exit_code,
            result.stdout.decode("utf-8", "replace"),
            result.stderr.decode("utf-8", "replace"),
            result.denials,
            outputs,
            new_tree,
            None if result.status == "ok" else result.status,
            record,
        )

    def _collect_tree(
        self, collected: Mapping[str, bytes], old: TreeManifest | None
    ) -> TreeManifest | None:
        """The new tree: every collected file, plus the old tree's protected entries."""
        if self._blobs is None:
            return None
        protected = self._policy.protected
        entries: dict[str, str] = {}
        for path, data in collected.items():
            if any(path == p or path.startswith(p + "/") for p in protected):
                continue
            entries[path] = self._blobs.put(data)
        if old is not None:
            for path, ref in old.entries.items():
                if any(path == p or path.startswith(p + "/") for p in protected):
                    entries[path] = ref
        return TreeManifest(entries)
