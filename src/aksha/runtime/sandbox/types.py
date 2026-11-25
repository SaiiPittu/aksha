"""Sandbox declarations (ADR-P12, P28): backends, environments and an exec tool's needs.

Built so far: ``SandboxBackendSpec``, ``EnvironmentSpec``, ``Strength``, ``SandboxPolicy``,
``SandboxNeeds``, and ``Availability`` with ``strong_enough``, which the policy gate reads
to refuse an exec tool that would run unconfined (ADR-P11 layer 7).
``SandboxPolicy.limits`` and ``narrow``, and ``SandboxNeeds.outputs``, arrive with the
sandbox primitive.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

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
    """An execution environment image: a pinned base, the inputs that key it, how to build it."""

    name: str
    base: str
    lock_inputs: tuple[str, ...]
    build_argv: tuple[str, ...]
    egress: str


@dataclass(frozen=True)
class SandboxPolicy:
    """What one exec may touch: writable and protected paths under ``/work``, and the network.

    ``protected`` is empty by default: which folders an agent's tools must never write is the
    agent's (or a sandbox extension's) declaration, never a runtime assumption.
    """

    writable: tuple[str, ...] = ("out", "tmp")
    protected: tuple[str, ...] = ()
    host_read: tuple[str, ...] = ()
    network: Literal["none", "allowlist"] = "none"


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
