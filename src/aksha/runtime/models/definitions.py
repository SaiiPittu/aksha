"""Definitions as dependencies (ADR-P05 amendment M2): the schema a value was produced under.

A value an extraction alias filled was produced under that alias's declared schema (its
``fields``), and a kind a classification alias decided under its declared questions (a
document-kind decision's question lists the kinds and their meanings, ADR-P07 G3). When
the definition changes, a value derived under the old one is stale even though no source
byte moved. So a fast-class call made under the alias's declared definition is recorded
as a read of the record ``definitions/<agent>/<alias>`` at version
:func:`alias_definition` (``ToolModelCalls.reads``), beside the tool's own reads, and the
``definitions`` source (``invalidation.definitions.DefinitionSource``) answers each such
record's current version from the loaded agent. A changed definition is then recorded by
the one writer of source facts like any moved record, and invalidation reopens exactly the
values read under it; a definition read is always a strong dependency.

A call that passes its own fields or questions, different from the declared ones, has its
definition in the tool's code, which the agent definition hash covers (ADR-P20 D7); it
records no definition read.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from aksha.runtime.canonical import sha256_hex
from aksha.runtime.models.types import ModelAlias

DEFINITIONS_SOURCE = "definitions"
"""The versioned source definition reads are recorded against, and its one collection."""


def definition_record(agent: str, alias: str) -> str:
    """The record a call of ``agent``'s ``alias`` reads: ``definitions/<agent>/<alias>``."""
    return f"{DEFINITIONS_SOURCE}/{agent}/{alias}"


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | frozenset | set):
        items = [_plain(v) for v in value]
        return sorted(items, key=repr) if isinstance(value, frozenset | set) else items
    if callable(value):
        return f"{getattr(value, '__module__', '')}.{getattr(value, '__qualname__', '')}"
    if isinstance(value, float):
        return repr(value)
    return value


def alias_definition(alias: ModelAlias) -> str:
    """The version of ``alias``'s declared definition: its class, fields and questions."""
    material = {
        "model_class": alias.model_class,
        "fields": _plain(alias.fields),
        "questions": _plain(alias.questions),
    }
    return "def-" + sha256_hex(material)[:16]
