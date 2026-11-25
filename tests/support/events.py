"""Probe event types and builders the tests append.

The probe types stand in for the types later primitives add to the core catalogue, so the
event store's rules can be exercised before those primitives exist.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue, EventTypeSpec
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.storage.chain import content_hash
from aksha.runtime.storage.rows import ArtifactRow

PROBE_NOTED = EventTypeSpec(
    name="probe.noted",
    schema={
        "type": "object",
        "properties": {"n": {"type": "integer"}, "text": {"type": "string"}},
        "required": ["n", "text"],
        "additionalProperties": False,
    },
    writer="tests",
    boundaries=frozenset({"U2"}),
    dedup_key="probe:{run}/{n}",
)
PROBE_FREE = EventTypeSpec(
    name="probe.free",
    schema={
        "type": "object",
        "properties": {"blob": {"type": "string"}, "items": {"type": "array"}},
        "additionalProperties": False,
    },
    writer="tests",
    boundaries=frozenset({"U2", "U3"}),
)
MEMORY_RECALLED = EventTypeSpec(
    name="memory.recalled",
    schema={"type": "object", "properties": {"n": {"type": "integer"}}},
    writer="memory",
    boundaries=frozenset({"U2"}),
)
"""An extension's event type: registered through ``register_extension``, never core."""

TEST_TYPES = (PROBE_NOTED, PROBE_FREE)

START = datetime(2026, 11, 27, 9, 0, 0, tzinfo=UTC)


def probe_catalogue() -> Catalogue:
    """The core catalogue plus the probe types."""
    return Catalogue((*CORE_EVENT_TYPES, *TEST_TYPES))


def probe(n: int, run: str = "R-0001", text: str = "t") -> NewEvent:
    """A ``probe.noted`` event whose dedup key is ``probe:{run}/{n}``."""
    return NewEvent("probe.noted", {"n": n, "text": text}, run=run)


ARTIFACT_CONTENT: Mapping[str, Any] = {"body": "hello"}


def artifact_row(
    ref: str = "note/A@v1", content: Mapping[str, Any] = ARTIFACT_CONTENT
) -> ArtifactRow:
    """A small artifact row for ``ref``, its ``content_hash`` computed from ``content``."""
    return ArtifactRow(
        ref=ref,
        kind="note",
        key="A",
        version=1,
        content=content,
        content_hash=content_hash(content),
        canonical_hash="c1",
    )


def artifact_row_with(content: Mapping[str, Any], ref: str = "note/A@v1") -> ArtifactRow:
    """:func:`artifact_row` with other ``content`` and the hash it claims left as the default's.

    For refusals checked before the hash (NUL, a float): the row is never stored.
    """
    return replace(artifact_row(ref), content=content)


def saved(ref: str = "note/A@v1", content: Mapping[str, Any] = ARTIFACT_CONTENT) -> NewEvent:
    """The ``artifact.saved`` event that pairs with :func:`artifact_row` of ``content``."""
    return NewEvent(
        "artifact.saved",
        {"ref": ref, "kind": "note", "key": "A", "version": 1,
         "content_hash": content_hash(content)},
    )  # fmt: skip


def run_started(run: str = "R-0001") -> NewEvent:
    """A ``run.started`` event for ``run``."""
    return NewEvent("run.started", {"agent": "a", "cause": "request"}, run=run)


def run_ended(run: str = "R-0001") -> NewEvent:
    """A ``run.ended`` event for ``run``."""
    return NewEvent("run.ended", {"outcome": "complete", "reason": "done"}, run=run)
