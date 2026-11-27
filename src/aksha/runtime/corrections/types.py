"""Correction values: the target, the evidence, the routing and the projected correction.

A correction names one target: a source **record** (``input/exports/reviews.csv``) at the
version the proposer saw, or an **artifact value** (``catalog.item/c01@v1``, the whole
content or one JSON pointer inside it). It carries the proposed value, at least one piece
of evidence, who proposed it (``person`` or ``agent:<name>``) and the **route** an
approval will take, fixed when it is proposed:

- ``upload``: the record is one of the owner's uploaded inputs; the approved text becomes
  that file's next version, recorded as a source change.
- ``outbox``: the record lives in a shared source whose system of record is configured
  (``CorrectionRouting.systems``); the approved text goes out through the outbox to that
  system (ADR-P03, ADR-P29), and its completion is recorded as a source change.
- ``artifact``: the value is saved as the slot's next version, origin ``correction``.

The domain owner is a role: ``CorrectionRouting.approver`` (``person`` by default), or a
per-source role in ``approvers``. Only a command sent under that role decides.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

CORRECTION_PREFIX = "K"
"""Correction IDs are ``K-0001``, counted from the owner's log."""

CORRECTION_TOOL = "correction.write"
"""The outbox tool name an approved source correction is enqueued under."""

TargetKind = Literal["record", "artifact"]
Route = Literal["upload", "outbox", "artifact"]
CorrectionState = Literal["proposed", "approved", "rejected", "applied", "failed"]
"""Where a correction stands; a projection. ``failed``: its outbox entry was abandoned."""

# Refusal codes.
EVIDENCE_REQUIRED = "evidence_required"
TARGET_UNKNOWN = "target_unknown"
TARGET_MOVED = "target_moved"
VALUE_INVALID = "value_invalid"
NO_SYSTEM_OF_RECORD = "no_system_of_record"
CORRECTION_UNKNOWN = "correction_unknown"
CORRECTION_CLOSED = "correction_closed"
NOT_DOMAIN_OWNER = "not_domain_owner"
UNCHANGED = "correction_unchanged"
PROPOSER_UNKNOWN = "proposer_unknown"


@dataclass(frozen=True)
class Target:
    """What a correction changes: a record (with its source) or an artifact value."""

    kind: TargetKind
    id: str
    version: str | None
    source: str | None = None
    path: str | None = None

    def as_json(self) -> dict[str, Any]:
        """The target as ``correction.proposed`` records it."""
        out: dict[str, Any] = {"kind": self.kind, "id": self.id, "version": self.version}
        if self.source is not None:
            out["source"] = self.source
        if self.path is not None:
            out["path"] = self.path
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Target:
        """Read a recorded target back."""
        version = data.get("version")
        return cls(
            kind=data["kind"],
            id=str(data["id"]),
            version=None if version is None else str(version),
            source=None if data.get("source") is None else str(data["source"]),
            path=None if data.get("path") is None else str(data["path"]),
        )

    def __str__(self) -> str:
        """The target as a person types it: the record ID, or the slot and its pointer."""
        if self.kind == "artifact":
            return self.id + (f"#{self.path}" if self.path else "")
        return self.id


@dataclass(frozen=True)
class Evidence:
    """Why the value is wrong: a statement, optionally pointing at what supports it."""

    text: str
    ref: str | None = None

    def as_json(self) -> dict[str, str]:
        """The evidence item as recorded."""
        return {"text": self.text} if self.ref is None else {"text": self.text, "ref": self.ref}


@dataclass(frozen=True)
class CorrectionRouting:
    """Where approved corrections go and who approves them (configuration, never stored)."""

    systems: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    """Source name to the system of record its corrections are written to (``outbox``)."""
    approver: str = "person"
    """The domain owner's role for every target with no role of its own."""
    approvers: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    """Source name (or artifact agent) to the domain owner's role for it."""

    def approver_for(self, scope: str | None) -> str:
        """The role that decides a correction to ``scope`` (a source or an agent)."""
        return self.approvers.get(scope or "", self.approver)


@dataclass(frozen=True)
class Affected:
    """One output a correction reopened, and what replaced it (a projection).

    ``state`` is ``replaced`` once a newer version of the slot is current, ``waiting``
    while its replacement waits on a person's review, ``pending`` while it is stale and
    not yet rebuilt, ``current`` when it settled by early cutoff with no new version.
    ``lineage`` is the replacement's recorded reads that lead back to the correction.
    """

    ref: str
    state: Literal["replaced", "waiting", "pending", "current"]
    replacement: str | None = None
    lineage: tuple[str, ...] = ()


@dataclass(frozen=True)
class Correction:
    """One correction as the log records it, with its derived state (never stored)."""

    correction: str
    target: Target
    value: Any
    value_hash: str
    evidence: tuple[Evidence, ...]
    by: str
    route: Route
    approver: str
    interrupt: str
    state: CorrectionState
    proposed_seq: int
    reason: str = ""
    system: str | None = None
    decided_by: str | None = None
    note: str = ""
    applied_seq: int | None = None
    applied: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    outbox_key: str | None = None
    affected: tuple[Affected, ...] = ()
