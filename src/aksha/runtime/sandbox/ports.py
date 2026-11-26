"""The ``Sandbox`` port (ADR-P12 decision 10, T3): request and response, nothing kept.

The runtime sends one ``BoxRequest`` (argv, inputs, the effective policy and its limits)
and gets one ``BoxResult`` back. ``open``, ``materialise``, ``wrap`` and ``collect`` in the
ADR's sketch are the backend's own steps inside :meth:`Sandbox.run` (amendment A1). The
backend reads a workspace tree's blobs through the ``BlobReader`` it is given, and hands
the running box to ``unit`` so the host can end it (T6, ADR-P18 D6). ``ExecAPI`` is what an
exec tool body gets as ``ToolContext.exec``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Protocol

from aksha.runtime.cancellation.ports import KillableUnit
from aksha.runtime.sandbox.types import Availability, BoxRequest, BoxResult, Strength, TreeManifest

if TYPE_CHECKING:
    from aksha.runtime.sandbox.exec import ExecOutcome


class BlobReader(Protocol):
    """Reads one blob by its ``sha256:`` ref."""

    def get(self, ref: str) -> bytes:
        """The bytes of ``ref``."""
        ...


class UnitHolder(Protocol):
    """Where a backend registers the running box, so a stop or a deadline can end it."""

    def attach(self, unit: KillableUnit) -> None:
        """The box ``unit`` is running now."""
        ...

    def detach(self) -> None:
        """The box has ended."""
        ...


class Sandbox(Protocol):
    """One sandbox backend (decision 10). ``probe`` is side-effect free and cached."""

    name: str
    strength: Strength

    def probe(self) -> Availability:
        """Whether this backend can run boxes here, and why not."""
        ...

    def version(self) -> str:
        """The backend's version, recorded on every exec (decision 9)."""
        ...

    def run(self, request: BoxRequest, blobs: BlobReader, unit: UnitHolder) -> BoxResult:
        """Run one box to completion and return what it did; keeps nothing afterwards."""
        ...


class ExecAPI(Protocol):
    """``ToolContext.exec``: run one program in a fresh box under the tool's policy."""

    def run(
        self,
        argv: Sequence[str],
        files: Mapping[str, bytes] | None = None,
        *,
        tree: TreeManifest | None = None,
        stdin: bytes = b"",
    ) -> ExecOutcome:
        """Run ``argv`` in ``/work`` built from ``tree`` and ``files``; never raises."""
        ...
