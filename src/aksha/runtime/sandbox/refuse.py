"""``RefuseSandbox``: the backend that runs nothing and is always there (decision 2)."""

from __future__ import annotations

from aksha.runtime.sandbox.errors import SANDBOX_UNAVAILABLE, SandboxRefused
from aksha.runtime.sandbox.ports import BlobReader, UnitHolder
from aksha.runtime.sandbox.types import Availability, BoxRequest, BoxResult, Strength

REFUSE = "refuse"


class RefuseSandbox:
    """Answers ``sandbox_unavailable`` to every request; never spawns."""

    name = REFUSE
    strength: Strength = "none"

    def __init__(self, reason: str = "the refuse backend runs nothing") -> None:
        """Refuse with ``reason`` (why no real backend was chosen)."""
        self.reason = reason

    def probe(self) -> Availability:
        """Never available."""
        return Availability(REFUSE, "none", False, self.reason)

    def version(self) -> str:
        """The runtime's own backend has no version of its own."""
        return "1"

    def run(self, request: BoxRequest, blobs: BlobReader, unit: UnitHolder) -> BoxResult:
        """Refuse."""
        raise SandboxRefused(SANDBOX_UNAVAILABLE, self.reason)
