"""The ``definitions`` source (ADR-P05 amendment M2): each agent's declared alias definitions.

A fast-class call made under an alias's declared definition records a read of
``definitions/<agent>/<alias>`` at that definition's version (``models.definitions``).
:class:`DefinitionSource` is a ``VersionedSource`` over the loaded agents that answers each
such record's current version, so the one writer of source facts records a changed
definition like any moved record (``source.changed``, cause ``command`` or ``recovery``)
and the values read under the old definition reopen, though no source byte moved. A
definition a loaded agent no longer declares reads as gone; a definition of an agent this
process did not load is not covered (``PartialSource``), so it is never taken as gone.
Given the loaded agents' definition hashes (``codes``), it also answers each agent's
``definitions/<agent>/~code`` record at that hash: the definition a tool's own schema lives
in (``models.definitions.code_record``). An agent with no hash given has no such record
covered.
Citation keying treats every definition read as strong (``keying.Keyed.strength``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from aksha.runtime.models.definitions import (
    CODE_RECORD,
    DEFINITIONS_SOURCE,
    alias_definition,
    code_record,
    definition_record,
)
from aksha.runtime.models.types import ModelAlias


class DefinitionSource:
    """The loaded agents' alias definitions as one versioned source (module doc)."""

    def __init__(
        self,
        aliases: Mapping[str, Iterable[ModelAlias]],
        codes: Mapping[str, str] | None = None,
    ) -> None:
        """Serve each agent's (by name) declared aliases, and its definition hash in ``codes``."""
        self._versions: dict[str, str] = {}
        self._agents = frozenset(aliases)
        self._coded = frozenset(codes or ())
        for agent, declared in aliases.items():
            for alias in declared:
                self._versions[definition_record(agent, alias.name)] = alias_definition(alias)
        for agent, hashed in (codes or {}).items():
            self._versions[code_record(agent)] = hashed

    @classmethod
    def of(cls, agents: Iterable[Any], codes: Mapping[str, str] | None = None) -> DefinitionSource:
        """Over agent definitions (each with ``name`` and ``model_aliases``) and their hashes."""
        return cls({a.name: tuple(a.model_aliases) for a in agents}, codes)

    @property
    def name(self) -> str:
        """``definitions``."""
        return DEFINITIONS_SOURCE

    @property
    def collections(self) -> tuple[str, ...]:
        """The one collection, ``definitions``."""
        return (DEFINITIONS_SOURCE,)

    def covers(self, record_id: str) -> bool:
        """Whether ``record_id`` is a definition of a loaded agent (``PartialSource``)."""
        parts = record_id.split("/")
        if len(parts) != 3 or parts[0] != DEFINITIONS_SOURCE:
            return False
        return parts[1] in (self._coded if parts[2] == CODE_RECORD else self._agents)

    def record_version(self, owner: str, record_id: str) -> str | None:
        """The definition's current version; None when its loaded agent no longer declares it."""
        return self._versions.get(record_id)

    def version(self, owner: str) -> str:
        """The whole source's version: a hash over every definition and its version."""
        h = hashlib.sha256()
        for rid, version in sorted(self._versions.items()):
            h.update(f"{rid}\0{version}\n".encode())
        return "defs-" + h.hexdigest()[:16]
