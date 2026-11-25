"""The backend-neutral half of ``append``: every check that needs no database.

``prepare_append`` turns a caller's boundary into a ``PreparedBatch``: payloads validated
and in canonical JSON, dedup keys rendered and checked, each artifact row paired with its
``artifact.saved`` event, and the owner rules applied. An adapter then only has to commit
the batch in one transaction, so SQLite and PostgreSQL cannot drift on these rules.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aksha.runtime.canonical import NUL, NotCanonical, canonical_bytes, freeze, nul_path
from aksha.runtime.events.catalogue import Catalogue
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.events.errors import DedupKeyError
from aksha.runtime.storage.chain import content_hash
from aksha.runtime.storage.errors import ArtifactWithoutEvent, StoreError
from aksha.runtime.storage.rows import ArtifactRow

SYSTEM_OWNER = "_system"
"""The owner of process-level facts. Only the recovery boundary (U7) writes to it."""

RECOVERY_BOUNDARY = "U7"


@dataclass(frozen=True)
class PreparedEvent:
    """A validated event, ready to insert: payload frozen and in canonical JSON."""

    type: str
    payload: Mapping[str, Any]
    payload_json: str
    run: str | None
    instance: str | None
    step: str | None
    session: str | None
    dedup_key: str | None
    schema_version: int

    def stored(self, *, seq: int, owner: str, at: str, boot: int) -> Event:
        """The ``Event`` this becomes once the store has given it a seq."""
        return Event(
            type=self.type,
            payload=self.payload,
            run=self.run,
            instance=self.instance,
            step=self.step,
            session=self.session,
            dedup_key=self.dedup_key,
            seq=seq,
            owner=owner,
            at=at,
            boot=boot,
            schema_version=self.schema_version,
        )


@dataclass(frozen=True)
class PreparedBatch:
    """One transaction boundary, checked. ``artifacts`` pairs each row with its event's index."""

    owner: str
    boundary: str
    events: tuple[PreparedEvent, ...]
    artifacts: tuple[tuple[int, ArtifactRow], ...]

    def dedup_keys(self) -> tuple[str, ...]:
        """Every dedup key in the batch, in batch order."""
        return tuple(e.dedup_key for e in self.events if e.dedup_key is not None)

    def first_conflict(self, existing: Mapping[str, Event]) -> Event | None:
        """Of the keys already stored (``existing``), the event for the first in batch order."""
        for key in self.dedup_keys():
            if key in existing:
                return existing[key]
        return None


def check_owner(owner: str) -> str:
    """Return ``owner``, or refuse an empty one: every read and write names its owner."""
    if not owner:
        raise StoreError("owner_required", "every event store call names an owner")
    return owner


def _refuse_field(name: str, value: str | None, what: str) -> None:
    """Refuse an envelope string holding NUL or outside the chain's encoding.

    NUL: PostgreSQL's ``text`` cannot hold it (``nul_in_field``). A lone surrogate: the
    chain cannot hash it and no driver can encode it (``not_canonical_field``). Both are
    refused here, before any transaction opens, so neither backend sees them.
    """
    if value is None:
        return
    if NUL in value:
        raise StoreError("nul_in_field", f"{what}: field {name!r} holds NUL (\\u0000)")
    try:
        canonical_bytes(value)
    except NotCanonical as e:
        raise StoreError("not_canonical_field", f"{what}: field {name!r}: {e.reason}") from None


def _refuse_nul_fields(e: NewEvent) -> None:
    """Refuse a bad envelope string in ``e`` (:func:`_refuse_field`)."""
    for name, value in (("run", e.run), ("instance", e.instance), ("step", e.step),
                        ("session", e.session), ("dedup_key", e.dedup_key)):  # fmt: skip
        _refuse_field(name, value, e.type)


def _refuse_nul_content(row: ArtifactRow) -> None:
    """Refuse NUL anywhere in an artifact row: PostgreSQL's ``text`` and ``jsonb`` refuse it."""
    found = nul_path(row.content)
    if found is None and any(NUL in str(v) for v in (row.ref, row.kind, row.key or "")):
        found = "ref, kind or key"
    if found is not None:
        raise StoreError("nul_in_content", f"artifact {row.ref!r}: NUL (\\u0000) at {found}")


def _check_artifact(row: ArtifactRow, saved: PreparedEvent) -> None:
    """Refuse an artifact row its ``artifact.saved`` event does not describe exactly (H6).

    The row's content must be in the chain's encoding (``not_canonical_content``) and hash
    to the row's ``content_hash`` (``content_hash_invalid``); the event's ``ref``,
    ``kind``, ``key``, ``version`` and ``content_hash`` must be the row's
    (``content_hash_mismatch``). The event's hash then covers the row, so ``verify()``
    finds an edited artifact row at its event.
    """
    _refuse_nul_content(row)
    for name, value in (("ref", row.ref), ("kind", row.kind), ("key", row.key)):
        _refuse_field(name, value, f"artifact {row.ref!r}")
    try:
        recomputed = content_hash(row.content)
    except NotCanonical as e:
        raise StoreError(
            "not_canonical_content", f"artifact {row.ref!r}: {e.path}: {e.reason}"
        ) from None
    if row.content_hash != recomputed:
        raise StoreError(
            "content_hash_invalid",
            f"artifact {row.ref!r}: content_hash {row.content_hash!r}, content hashes to"
            f" {recomputed!r}",
        )
    described = tuple(saved.payload.get(k) for k in ("ref", "kind", "key", "version"))
    if described != (row.ref, row.kind, row.key, row.version) or (
        saved.payload.get("content_hash") != row.content_hash
    ):
        raise StoreError(
            "content_hash_mismatch",
            f"artifact {row.ref!r}: its artifact.saved event describes another version",
        )


def _prepare_event(catalogue: Catalogue, owner: str, boundary: str, e: NewEvent) -> PreparedEvent:
    spec = catalogue.get(e.type)
    _refuse_nul_fields(e)
    catalogue.check_boundary(e.type, boundary)
    payload_json = catalogue.validate_payload(e.type, e.payload)
    payload = freeze(json.loads(payload_json))
    rendered = catalogue.render_dedup_key(e.type, {**e.context(), "owner": owner}, payload)
    if rendered is None and e.dedup_key is not None:
        raise DedupKeyError("dedup_key_not_allowed", f"{e.type} has no dedup-key template")
    if rendered is not None and e.dedup_key is not None and e.dedup_key != rendered:
        raise DedupKeyError(
            "dedup_key_mismatch", f"{e.type}: caller key {e.dedup_key!r}, template {rendered!r}"
        )
    return PreparedEvent(
        type=e.type,
        payload=payload,
        payload_json=payload_json,
        run=e.run,
        instance=e.instance,
        step=e.step,
        session=e.session,
        dedup_key=rendered,
        schema_version=spec.schema_version,
    )


def prepare_append(
    catalogue: Catalogue,
    owner: str,
    boundary: str,
    events: Sequence[NewEvent],
    artifacts: Sequence[ArtifactRow],
) -> PreparedBatch:
    """Check one boundary against the catalogue and the owner rules; nothing touches storage.

    Refuses an empty owner or boundary, the system owner outside recovery (and recovery
    outside the system owner), every catalogue refusal, a dedup key repeated within the
    boundary, an artifact row with no ``artifact.saved`` event for its ref or one that
    event does not describe (:func:`_check_artifact`), NUL in any stored string
    (``nul_in_field``, ``nul_in_content``) and a lone surrogate in the owner or an
    envelope string (``not_canonical_field``), so both backends refuse alike, and before
    either opens a transaction.
    """
    check_owner(owner)
    _refuse_field("owner", owner, "the boundary")
    _refuse_field("boundary", boundary, "the boundary")
    if not events:
        raise StoreError("empty_boundary", "a transaction boundary holds at least one event")
    if (owner == SYSTEM_OWNER) != (boundary == RECOVERY_BOUNDARY):
        raise StoreError(
            "system_owner_reserved",
            f"the system owner {SYSTEM_OWNER!r} writes the recovery boundary U7, and only it",
        )
    prepared = tuple(_prepare_event(catalogue, owner, boundary, e) for e in events)
    keys = [p.dedup_key for p in prepared if p.dedup_key is not None]
    if len(keys) != len(set(keys)):
        raise DedupKeyError("dedup_key_repeated_in_boundary", "one boundary repeats a dedup key")
    saved_at = {
        str(p.payload["ref"]): i for i, p in enumerate(prepared) if p.type == "artifact.saved"
    }
    paired: list[tuple[int, ArtifactRow]] = []
    for row in artifacts:
        _refuse_nul_content(row)
        if row.ref not in saved_at:
            raise ArtifactWithoutEvent(row.ref)
        _check_artifact(row, prepared[saved_at[row.ref]])
        paired.append((saved_at[row.ref], row))
    return PreparedBatch(owner, boundary, prepared, tuple(paired))
