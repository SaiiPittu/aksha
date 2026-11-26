"""The three memory kinds (ADR-P09 D2), and what a record and its tombstone hold.

Memory records are artifacts, so versions, hashes, the tenant wall, idempotent saves,
lineage and invalidation all come from the artifact store.

A record is ``{subject, text, source: {cite: ...}}`` and a tombstone (D9) is
``{subject, forgotten: true}``. A preference must cite a person's statement; a fact or a
procedure cites a statement or an artifact version. The cite rules are declared data
(``CitePolicy``) checked by the one verifier at save (ADR-P13), never by memory code.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.api import ArtifactKind, CitePolicy

_SUBJECT: Mapping[str, Any] = {"type": "string", "minLength": 1, "maxLength": 200}
_RECORD: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "subject": _SUBJECT,
        "text": {"type": "string", "minLength": 1},
        "source": {
            "type": "object",
            "properties": {"cite": {"type": "object"}},
            "required": ["cite"],
            "additionalProperties": False,
        },
    },
    "required": ["subject", "text", "source"],
    "additionalProperties": False,
}
_TOMBSTONE: Mapping[str, Any] = {
    "type": "object",
    "properties": {"subject": _SUBJECT, "forgotten": {"const": True}},
    "required": ["subject", "forgotten"],
    "additionalProperties": False,
}
SCHEMA: Mapping[str, Any] = {"oneOf": [dict(_RECORD), dict(_TOMBSTONE)]}
"""A memory record or its tombstone."""

STATED = CitePolicy(field="/source", forms=frozenset({"statement"}))
"""A preference cites the person's statement that created it."""

STATED_OR_DERIVED = CitePolicy(field="/source", forms=frozenset({"statement", "artifact"}))
"""A fact or a procedure cites a statement or an artifact version."""

PREFERENCE = ArtifactKind("preference", SCHEMA, keyed=True, cites=(STATED,))
FACT = ArtifactKind("fact", SCHEMA, keyed=True, cites=(STATED_OR_DERIVED,))
PROCEDURE = ArtifactKind("procedure", SCHEMA, keyed=True, cites=(STATED_OR_DERIVED,))

KINDS: tuple[ArtifactKind, ...] = (PREFERENCE, FACT, PROCEDURE)
"""What the extension contributes; full names ``memory.preference`` and so on."""


def is_tombstone(content: Mapping[str, Any]) -> bool:
    """Whether ``content`` is a forgotten record's tombstone."""
    return content.get("forgotten") is True
