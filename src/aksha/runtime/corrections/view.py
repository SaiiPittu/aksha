"""Corrections read from the log (ADR-P05 C1): a projection, never stored.

The facts are ``correction.proposed`` (with its ``interrupt.raised{kind: correction}``),
``correction.decided``, the outbox facts of an ``outbox`` correction, and
``correction.applied``, which commits with the source change or the artifact version
that applied it. The state follows them: ``proposed``, then ``rejected`` or ``approved``,
then ``applied`` (or ``failed`` when its outbox entry was abandoned).

:func:`outcome` adds what the correction reopened and what replaced it. The affected
outputs are the latest versions the invalidation view holds stale just after the
correction applied and did not hold stale just before it; for each, the replacement is
the newest later version of its slot on the same line (or a correction's), and its
lineage is the replacement's recorded reads that name the corrected input or another
affected output's slot. Nothing about the outcome is stored: the replacements' own
saves, with their reads, are the record, and this joins them to the correction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Any

from aksha.runtime.artifacts.types import ArtifactRef, InputRef
from aksha.runtime.corrections.types import (
    CORRECTION_TOOL,
    Affected,
    Correction,
    CorrectionState,
    Evidence,
    Target,
)
from aksha.runtime.invalidation.types import STALE_STATES
from aksha.runtime.invalidation.view import InvalidationView, invalidation_view
from aksha.runtime.outbox.outbox import entries
from aksha.runtime.projections.view import LogView

PROPOSED = "correction.proposed"
DECIDED = "correction.decided"
APPLIED = "correction.applied"


def change_set(correction: str) -> str:
    """The ``source.changed{change_set}`` an applied source correction records."""
    return f"correction:{correction}"


def corrections(view: LogView) -> Mapping[str, Correction]:
    """Every correction of the owner by ID, in proposal order, with its state."""
    found: dict[str, dict[str, Any]] = {}
    interrupts: dict[str, str] = {}
    for e in view.events:
        p = e.payload
        if e.type == "interrupt.raised" and p.get("kind") == "correction":
            interrupts[str(p["correction"])] = str(p["interrupt"])
        elif e.type == PROPOSED:
            found[str(p["correction"])] = {"p": p, "seq": e.seq}
        elif e.type == DECIDED and str(p["correction"]) in found:
            found[str(p["correction"])]["d"] = p
        elif e.type == APPLIED and str(p["correction"]) in found:
            found[str(p["correction"])]["a"] = (p, e.seq)
    outbox = entries(view)
    out: dict[str, Correction] = {}
    for ident, held in found.items():
        p, decided = held["p"], held.get("d")
        applied = held.get("a")
        state: CorrectionState = "proposed"
        if decided is not None:
            state = "approved" if decided["decision"] == "approved" else "rejected"
        key = None if applied is None else applied[0].get("outbox_key")
        if applied is not None:
            state = "applied"
        elif state == "approved" and p["route"] == "outbox":
            mine = (x for x in outbox.values() if x.tool == CORRECTION_TOOL)
            entry = next((x for x in mine if _names(x.request, ident)), None)
            if entry is not None:
                key = entry.key
                if entry.state in ("abandoned", "outcome_unknown"):
                    state = "failed"
        out[ident] = Correction(
            correction=ident,
            target=Target.from_json(p["target"]),
            value=p["value"],
            value_hash=str(p["value_hash"]),
            evidence=tuple(Evidence(str(x["text"]), x.get("ref")) for x in p["evidence"]),
            by=str(p["by"]),
            route=p["route"],
            approver=str(p["approver"]),
            interrupt=interrupts.get(ident, ""),
            state=state,
            proposed_seq=int(held["seq"]),
            reason=str(p.get("reason", "")),
            system=None if p.get("system") is None else str(p["system"]),
            decided_by=None if decided is None else str(decided["by"]),
            note="" if decided is None else str(decided.get("note", "")),
            applied_seq=None if applied is None else int(applied[1]),
            applied=MappingProxyType({}) if applied is None else MappingProxyType(dict(applied[0])),
            outbox_key=None if key is None else str(key),
        )
    return MappingProxyType(out)


def _names(request: Mapping[str, Any], ident: str) -> bool:
    return request.get("correction") == ident


def outcome(view: LogView, ident: str) -> Correction | None:
    """The correction ``ident`` with what it reopened and what replaced it (module doc)."""
    found = corrections(view).get(ident)
    if found is None or found.applied_seq is None:
        return found
    start = _start(view, found)
    before = invalidation_view(view.upto(start - 1))
    after = invalidation_view(view.upto(found.applied_seq))
    now = invalidation_view(view)
    corrected = found.applied.get("version")
    out: list[Affected] = []
    reopened = [
        s
        for s in after.latest()
        if s.freshness in STALE_STATES
        and str(s.ref) != corrected
        and not (str(s.ref) in before.versions and before.versions[str(s.ref)].stale)
    ]
    slots = {s.ref.slot() for s in reopened}
    for state in reopened:
        out.append(_affected(view, now, state.ref, state.line, found, slots))
    return replace(found, affected=tuple(out))


def _start(view: LogView, c: Correction) -> int:
    """The first seq of the transaction that applied ``c``."""
    assert c.applied_seq is not None
    if c.route == "artifact":
        ref = c.applied.get("version")
        for e in view.events:
            if e.type == "artifact.saved" and e.payload.get("ref") == ref:
                return e.seq
        return c.applied_seq
    name = change_set(c.correction)
    for e in view.events:
        if e.type == "source.changed" and e.payload.get("change_set") == name:
            return e.seq
    return c.applied_seq


def _affected(
    view: LogView,
    now: InvalidationView,
    ref: ArtifactRef,
    line: tuple[str, ...],
    c: Correction,
    slots: set[tuple[str, str | None]],
) -> Affected:
    mine = frozenset(line)
    assert c.applied_seq is not None
    later = [
        s
        for s in now.versions.values()
        if s.ref.slot() == ref.slot()
        and s.ref.version > ref.version
        and s.event_seq > c.applied_seq
        and (not s.line or bool(frozenset(s.line) & mine))
    ]
    held = now.versions.get(str(ref))
    if held is not None and held.open_revision is not None:
        return Affected(str(ref), "waiting", str(held.open_revision))
    current_later = [s for s in later if s.is_latest and not s.stale]
    if current_later:
        new = current_later[-1]
        return Affected(str(ref), "replaced", str(new.ref), _lineage(view, new.ref, c, slots))
    if held is not None and not held.stale:
        return Affected(str(ref), "current")
    return Affected(str(ref), "pending", None if not later else str(later[-1].ref))


def _lineage(
    view: LogView, ref: ArtifactRef, c: Correction, slots: set[tuple[str, str | None]]
) -> tuple[str, ...]:
    """The replacement's reads that name the corrected input or another reopened slot."""
    text = str(ref)
    reads: tuple[InputRef, ...] = ()
    for e in view.events:
        if e.type == "artifact.saved" and e.payload.get("ref") == text:
            reads = tuple(InputRef.from_json(r) for r in e.payload.get("reads", ()))
            break
    corrected = c.applied.get("version")
    out: list[str] = []
    for r in reads:
        if r.kind == "record" and r.id.partition("#")[0] == c.target.id:
            out.append(f"{r.id}@{r.version}")
        elif r.kind == "artifact":
            whole = ArtifactRef.from_input(r)
            if str(whole) == corrected or whole.slot() in slots:
                out.append(str(whole))
    return tuple(out)
