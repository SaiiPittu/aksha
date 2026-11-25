"""Artifact values declared by agents and extensions: ``ArtifactKind`` and ``RelationKind``.

ADR-P04. Also ``SaveRequest``, what a tool returns to ask for a save (its ``reads`` and
``relations`` fields arrive with the store). ``ArtifactRef``, ``InputRef``, ``WriterId`` and
``ArtifactVersion`` arrive with the artifact store, and so do ``ArtifactKind``'s ``validate``,
``cites`` and ``choice`` fields, whose value types (ADR-P04 S1, ADR-P13) are not built yet.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from aksha.runtime.tasks.types import Check


@dataclass(frozen=True)
class ArtifactKind:
    """A kind of artifact: its schema and how the store treats its versions.

    ``name`` is local (``line``); the loader prefixes the declarer's namespace. ``schema`` is
    a JSON Schema mapping or a dataclass. ``derived`` is a pure function over other kinds.
    """

    name: str
    schema: type | Mapping[str, Any]
    keyed: bool = False
    reviewable: bool = False
    lifecycle: bool = False
    derived: Callable[..., Mapping[str, Any]] | None = None
    readiness: tuple[Check, ...] = ()
    writers: Literal["one", "chain"] = "one"
    canon_exclude: tuple[str, ...] = ()
    list_keys: Mapping[str, str] = field(default_factory=dict)
    canonicalize: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    save: Literal["any", "tool_only"] = "any"
    summary: str | None = None


@dataclass(frozen=True)
class RelationKind:
    """A typed relation between artifacts (v2 ``EdgeKind``).

    Each end is a kind (local, or ``ns.kind``) or ``record:<collection>``.
    """

    name: str
    src: tuple[str, ...]
    dst: tuple[str, ...]
    labels: tuple[str, ...] = ()
    symmetric: bool = False
    transitive: bool = False
    cites: Literal["one", "per_end"] = "one"


@dataclass(frozen=True)
class SaveRequest:
    """A save a tool asks for; the artifact store does the save and records its reads.

    ``kind`` is a full or local kind name; ``key`` is None for an unkeyed kind.
    """

    kind: str
    key: str | None
    content: Mapping[str, Any]
