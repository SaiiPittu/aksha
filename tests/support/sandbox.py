"""A fake sandbox backend for runtime tests: it records each request and answers from a rule.

The fake confines nothing and runs nothing; it stands in for a backend wherever the test is
about the runtime around the port (selection, policy, records, outputs), never about
confinement. Confinement is the bubblewrap conformance suite's job.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from aksha.runtime.sandbox.ports import BlobReader, UnitHolder
from aksha.runtime.sandbox.types import Availability, BoxRequest, BoxResult, Strength

Rule = Callable[[BoxRequest, BlobReader], BoxResult]


def echo_rule(request: BoxRequest, blobs: BlobReader) -> BoxResult:
    """Exit 0, print the argv, and collect every input file back under its own path."""
    collected = dict(request.files)
    if request.tree is not None:
        collected.update({p: blobs.get(r) for p, r in request.tree.entries.items()})
    return BoxResult("ok", 0, " ".join(request.argv).encode(), b"", collected=collected)


@dataclass
class FakeSandbox:
    """A backend that answers every request with ``rule`` (module doc)."""

    name: str = "fake"
    strength: Strength = "os-jail"
    available: bool = True
    rule: Rule = echo_rule
    requests: list[BoxRequest] = field(default_factory=list)
    probes: int = 0

    def probe(self) -> Availability:
        self.probes += 1
        return Availability(self.name, self.strength, self.available,
                            "" if self.available else "fake: switched off")  # fmt: skip

    def version(self) -> str:
        return "fake-1"

    def run(self, request: BoxRequest, blobs: BlobReader, unit: UnitHolder) -> BoxResult:
        self.requests.append(request)
        return self.rule(request, blobs)
