"""Diff reviews and approvals as read from the log (ADR-P05 D1, D9). Every function is pure.

A diff review is an ``interrupt.raised{kind: review}`` naming the old version, the pending
revision and the card's hash. It is open until ``review.accepted`` or ``review.rejected``
(with ``interrupt.resolved``) or ``interrupt.voided`` closes it. Approvals are ADR-P17's;
invalidation reads ``approval.granted`` by name (its targets are artifact refs) and writes
``approval.revoked``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Literal

from aksha.runtime.artifacts.types import ArtifactRef
from aksha.runtime.canonical import sha256_hex
from aksha.runtime.invalidation.types import Hunk, ReadStatus
from aksha.runtime.projections.view import LogView

ReviewOutcome = Literal["open", "accepted", "rejected", "voided"]

REVIEW = "review"
"""The ``kind`` of an ``interrupt.raised`` that is a diff review."""


@dataclass(frozen=True)
class ReviewRecord:
    """One diff review and how it ended; ``seq`` is the raise, ``closed_seq`` the close."""

    interrupt: str
    old: ArtifactRef
    revision: ArtifactRef
    card_hash: str
    run: str | None
    instance: str | None
    seq: int
    outcome: ReviewOutcome = "open"
    closed_seq: int | None = None


def reviews(view: LogView) -> tuple[ReviewRecord, ...]:
    """Every diff review in raise order, with its outcome."""
    found: dict[str, ReviewRecord] = {}
    for e in view.events:
        p = e.payload
        if e.type == "interrupt.raised" and p.get("kind") == REVIEW:
            ident = str(p["interrupt"])
            found[ident] = ReviewRecord(
                ident, ArtifactRef.parse(str(p["old"])), ArtifactRef.parse(str(p["revision"])),
                str(p["card_hash"]), e.run, e.instance, e.seq,
            )  # fmt: skip
            continue
        outcome = _closing(e.type)
        ident = str(p.get("interrupt", ""))
        if outcome is not None and ident in found and found[ident].outcome == "open":
            r = found[ident]
            found[ident] = ReviewRecord(
                r.interrupt, r.old, r.revision, r.card_hash, r.run, r.instance, r.seq,
                outcome, e.seq,
            )  # fmt: skip
    return tuple(found.values())


def _closing(event_type: str) -> ReviewOutcome | None:
    return {
        "review.accepted": "accepted",
        "review.rejected": "rejected",
        "interrupt.voided": "voided",
    }.get(event_type)  # type: ignore[return-value]


def open_reviews(view: LogView) -> Mapping[str, ReviewRecord]:
    """The open diff reviews, keyed by their old version's ref."""
    return MappingProxyType({str(r.old): r for r in reviews(view) if r.outcome == "open"})


def card_hash(
    old: ArtifactRef,
    revision: ArtifactRef,
    changed: tuple[ReadStatus, ...],
    via: tuple[str, ...],
    hunks: tuple[Hunk, ...],
) -> str:
    """The hash of everything a diff review shows: what the person accepts (D1)."""
    card = {
        "old": str(old),
        "revision": str(revision),
        "changed_reads": [r.as_json() for r in changed],
        "via": list(via),
        "hunks": [h.as_json() for h in hunks],
    }
    return "sha256:" + sha256_hex(card)


# --- approvals (ADR-P17's facts, read by name) -----------------------------------------


@dataclass(frozen=True)
class ApprovalRecord:
    """One granted approval: its ID, the artifact refs it covers, and whether it was revoked."""

    approval: str
    targets: tuple[str, ...]
    seq: int
    revoked: bool = False
    used: bool = False


def approvals(view: LogView) -> tuple[ApprovalRecord, ...]:
    """Every granted approval, in grant order, with its revocation and use.

    An approval is used once an outbox entry names it (``outbox.enqueued{approval_id}``);
    a used approval is never revoked (ADR-P17 D7).
    """
    granted: dict[str, ApprovalRecord] = {}
    for e in view.events:
        p = e.payload
        if e.type == "approval.granted":
            ident = str(p["approval"])
            granted[ident] = ApprovalRecord(ident, _targets(p), e.seq)
        elif e.type == "approval.revoked" and str(p["approval"]) in granted:
            r = granted[str(p["approval"])]
            granted[r.approval] = replace(r, revoked=True)
        elif e.type == "outbox.enqueued" and str(p.get("approval_id")) in granted:
            r = granted[str(p["approval_id"])]
            granted[r.approval] = replace(r, used=True)
    return tuple(granted.values())


def _targets(payload: Mapping[str, Any]) -> tuple[str, ...]:
    targets = payload.get("targets") or ()
    return tuple(str(t) for t in targets)


def approved_refs(view: LogView) -> frozenset[str]:
    """Every artifact ref a person ever approved (revoked or not): human-reviewed (D1)."""
    return frozenset(t for a in approvals(view) for t in a.targets)
