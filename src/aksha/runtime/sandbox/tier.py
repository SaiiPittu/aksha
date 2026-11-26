"""``ExecutionTier``: what a process gives its runs of tier 3 (ADR-P12 T1 to T3, P26, P28).

The sandbox backend chosen at start and every probe made (decision 2), the run ceiling
every exec policy narrows from, the network adapters under the policed HTTP client (None
keeps the process offline: every request is refused ``network_off``), the egress policies
and environment specs the loaded extensions declare, by full name, and the environment
keys a person granted. The composition root builds one; the agent loop reads it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from aksha.runtime.egress.ports import Network
from aksha.runtime.egress.types import EgressPolicy
from aksha.runtime.sandbox.ports import Sandbox
from aksha.runtime.sandbox.types import Availability, EnvironmentSpec, ExecPolicy

RUN_CEILING = ExecPolicy.of(writable=(".",))
"""The default run ceiling: any path may be declared writable, the network is off, and the
limits are the defaults; each tool's needs narrow it."""


@dataclass(frozen=True)
class ExecutionTier:
    """Tier 3 as one process sees it (module doc)."""

    box: Sandbox | None = None
    probes: tuple[Availability, ...] = ()
    ceiling: ExecPolicy = RUN_CEILING
    network: Network | None = None
    egress: Mapping[str, EgressPolicy] = field(default_factory=lambda: MappingProxyType({}))
    environments: Mapping[str, EnvironmentSpec] = field(
        default_factory=lambda: MappingProxyType({})
    )
    approved_images: frozenset[str] = frozenset()
    declared: Availability | None = None
    """An outer box the operator attests the process runs in (``AKSHA_SANDBOX=<name>:<strength>``
    for a name no built-in backend has). It lets a headless run start; it runs no program,
    so an exec tool still needs a built-in backend."""

    @property
    def gate_availability(self) -> Availability | None:
        """What the policy gate and the headless check read: the backend, else the declaration."""
        return self.availability if self.box is not None else self.declared

    @property
    def availability(self) -> Availability | None:
        """The chosen backend's probe, or None when no backend was chosen."""
        if self.box is None:
            return None
        return next((p for p in self.probes if p.backend == self.box.name), self.box.probe())

    def record(self) -> dict[str, Any]:
        """What ``run.started`` records of the sandbox (decision 9)."""
        chosen = self.availability
        out: dict[str, Any] = {
            "probes": [
                {
                    "backend": p.backend,
                    "strength": p.strength,
                    "available": p.available,
                    **({"reason": p.reason} if p.reason else {}),
                }
                for p in self.probes
            ],
            "ceiling_hash": self.ceiling.hash_ref(),
            "network": "on" if self.network is not None else "off",
        }
        if self.approved_images:
            out["approved_images"] = sorted(self.approved_images)
        if self.box is None and self.declared is not None:
            out.update({"backend": self.declared.backend, "strength": self.declared.strength,
                        "available": self.declared.available, "declared": True})  # fmt: skip
        if chosen is not None and self.box is not None:
            out["backend"] = self.box.name
            out["strength"] = chosen.strength
            out["available"] = chosen.available
            if chosen.available:
                out["version"] = self.box.version()
        return out
