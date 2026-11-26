"""Egress policy (ADR-P26): which hosts a network-reading tool may reach.

Deny by default (amendment A1): an empty ``allow_hosts`` reaches no host; ``"*"`` names
any public host. The check is :func:`aksha.runtime.egress.check.check_destination`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EgressPolicy:
    """A named egress policy. Empty ``allow_hosts`` reaches nothing; ``("*",)`` any public host."""

    name: str
    schemes: frozenset[str] = frozenset({"https"})
    allow_hosts: tuple[str, ...] = ()
    deny_hosts: tuple[str, ...] = ()
    allow_private: bool = False
