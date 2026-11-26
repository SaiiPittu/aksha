"""Sandbox refusals (ADR-P12): a policy that would widen, a backend that cannot run."""

from __future__ import annotations

from aksha.runtime.errors import AkshaError

SANDBOX_UNAVAILABLE = "sandbox_unavailable"
"""No available backend meets the tool's strength; nothing spawns (decision 2)."""


class PolicyWidened(AkshaError):
    """A narrower layer asked for more than the layer above allows (decision 2)."""

    default_code = "policy_widened"


class SandboxRefused(AkshaError):
    """A backend refused to run a request: ``sandbox_unavailable`` and its siblings."""

    default_code = SANDBOX_UNAVAILABLE
