"""Egress policy (ADR-P26): which hosts a network-reading tool may reach.

Built so far: the declared ``EgressPolicy``. ``check_destination`` and the ``HttpClient``
port arrive with the primitive.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EgressPolicy:
    """A named egress policy. Empty ``allow_hosts`` means any public host."""

    name: str
    schemes: frozenset[str] = frozenset({"https"})
    allow_hosts: tuple[str, ...] = ()
    deny_hosts: tuple[str, ...] = ()
    allow_private: bool = False
