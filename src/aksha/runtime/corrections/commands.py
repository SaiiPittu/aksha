"""The correction commands and applying an approved correction (ADR-P05 C1, ADR-P17 C1).

Each command goes through the session API's one write path (``session.commands.dispatch``),
so its facts commit with ``command.accepted`` under the command's idempotency key (U4).

1. ``propose_correction`` records ``correction.proposed`` and an owner-level
   ``interrupt.raised{kind: correction}`` naming the domain owner's role. The caller has
   resolved the target against the live sources or artifacts (its current version) and
   the route. Refusals: ``evidence_required`` (no evidence), ``value_invalid`` (a record
   value that is not text), ``correction_unchanged`` (the value is what the target holds).
2. ``decide_correction`` records the domain owner's ``correction.decided`` and
   ``interrupt.resolved``. Approving an ``outbox`` correction enqueues its write in the
   same transaction (ADR-P03 D1: the entry commits with the decision); approving an
   ``artifact`` correction saves the corrected value as the slot's next version, origin
   ``correction``, and records ``correction.applied`` with it, with the revocations that
   version confirms (ADR-P05 D9). Refusals: ``correction_unknown``, ``correction_closed``
   (already decided), ``not_domain_owner`` (the sender's role is not the approver),
   ``target_moved`` (the target changed since the proposal: propose again against it).
3. :func:`apply_upload` and :func:`apply_outbox` finish an approved source correction:
   the first writes the new text as the owner's next version of the uploaded file, the
   second waits for its outbox entry's completion; each then records what moved with the
   one writer of source facts (``record_source_changes``, change set
   ``correction:<id>``) and ``correction.applied``, in one source-change transaction (U8).
   Both are idempotent and run again after a crash: an approved correction with no
   ``correction.applied`` is applied again, a file already written is not written again,
   and a file uploaded anew since the decision is never overwritten.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from aksha.runtime.artifacts.errors import SaveRefused
from aksha.runtime.artifacts.hashing import canonical_hash, reads_hash
from aksha.runtime.artifacts.store import checked_content, computed
from aksha.runtime.artifacts.types import ArtifactKind, ArtifactRef, ArtifactVersion
from aksha.runtime.artifacts.versions import current, version
from aksha.runtime.canonical import canonical_bytes, thaw
from aksha.runtime.corrections.types import (
    CORRECTION_CLOSED,
    CORRECTION_PREFIX,
    CORRECTION_TOOL,
    CORRECTION_UNKNOWN,
    EVIDENCE_REQUIRED,
    NOT_DOMAIN_OWNER,
    TARGET_MOVED,
    UNCHANGED,
    VALUE_INVALID,
    Correction,
    Evidence,
    Route,
    Target,
)
from aksha.runtime.corrections.view import APPLIED, DECIDED, PROPOSED, change_set, corrections
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.ports import IdSource
from aksha.runtime.invalidation.confirm import confirmations
from aksha.runtime.invalidation.ports import InputWorkspace, QueryRunner, VersionedSource
from aksha.runtime.invalidation.sources import (
    SOURCE_CHANGE_BOUNDARY,
    record_source_changes,
    source_facts,
)
from aksha.runtime.invalidation.view import CORRECTION_ORIGIN
from aksha.runtime.outbox.outbox import enqueue, entries
from aksha.runtime.outbox.types import EffectSpec
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.session.commands import derive_command_id, dispatch
from aksha.runtime.session.types import (
    Command,
    CommandAccepted,
    CommandReceipt,
    CommandRefused,
    CommandSpec,
)
from aksha.runtime.storage.errors import AlreadyRecorded
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.storage.rows import ArtifactRow

DECISION_BOUNDARY = "U4"

CORRECTION_EFFECT = EffectSpec(
    system=None,
    key_fields=("/correction", "/record", "/value_hash"),
    semantics="at_least_once",
    record_fields=("/record",),
)
"""How an approved source correction is written (ADR-P03): keyed by what it means."""

VersionNow = Callable[[Target], str | None]
"""The target's version now: a record's from its source, an artifact slot's current ref."""


def value_hash(value: Any) -> str:
    """``sha256:`` over a record's text bytes (its version, for an input file) or canonical JSON."""
    data = value.encode("utf-8") if isinstance(value, str) else canonical_bytes(value)
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _command(
    name: str, owner: str, args: dict[str, object], command_id: str | None, by: str
) -> Command:
    command = Command(name, owner, owner, {**args, "by": by}, command_id)
    if command_id is None:
        command = Command(name, owner, owner, command.args, derive_command_id(command))
    return command


# --- propose ------------------------------------------------------------------------------


def propose_spec(
    ids: IdSource,
    target: Target,
    *,
    route: Route,
    approver: str,
    system: str | None = None,
    held: Any = None,
) -> CommandSpec:
    """The ``propose_correction`` command for ``target`` (module doc, item 1).

    ``held`` is what the target holds now (a record's text, an artifact value), so a
    proposal of the same value is refused.
    """

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        value = command.args["value"]
        evidence = [Evidence(str(x["text"]), x.get("ref")) for x in command.args["evidence"]
                    if str(x.get("text", "")).strip()]  # fmt: skip
        if not evidence:
            return CommandRefused(EVIDENCE_REQUIRED, "a correction carries at least one piece "
                                  "of evidence")  # fmt: skip
        if target.kind == "record" and not isinstance(value, str):
            return CommandRefused(VALUE_INVALID, "a record's correction is its full new text")
        if held is not None and thaw(held) == thaw(value):
            return CommandRefused(UNCHANGED, f"{target} already holds that value")
        ident = ids.next(view.owner, CORRECTION_PREFIX)
        payload: dict[str, Any] = {
            "correction": ident,
            "target": target.as_json(),
            "value": thaw(value),
            "value_hash": value_hash(thaw(value)),
            "evidence": [e.as_json() for e in evidence],
            "by": str(command.args["by"]),
            "route": route,
            "approver": approver,
        }
        reason = command.args.get("reason")
        if reason:
            payload["reason"] = str(reason)
        if system is not None:
            payload["system"] = system
        raised = {"interrupt": ids.next(view.owner, "I"), "kind": "correction",
                  "correction": ident, "approver": approver,
                  "label": f"correction to {target}"}  # fmt: skip
        return CommandAccepted((NewEvent(PROPOSED, payload),
                                NewEvent("interrupt.raised", raised)))  # fmt: skip

    return CommandSpec("propose_correction", DECISION_BOUNDARY, decide)


def propose_correction(
    store: EventStore,
    owner: str,
    ids: IdSource,
    target: Target,
    value: Any,
    evidence: Sequence[Evidence],
    *,
    route: Route,
    approver: str,
    by: str,
    reason: str = "",
    system: str | None = None,
    held: Any = None,
    command_id: str | None = None,
) -> CommandReceipt:
    """Record a proposed correction (module doc, item 1)."""
    args: dict[str, object] = {
        "target": target.as_json(),
        "value": value,
        "evidence": [e.as_json() for e in evidence],
        "reason": reason,
    }
    command = _command("propose_correction", owner, args, command_id, by)
    spec = propose_spec(ids, target, route=route, approver=approver, system=system, held=held)
    return dispatch(store, command, spec)


# --- decide -------------------------------------------------------------------------------


def _open(view: LogView, ident: str, by: str) -> Correction | CommandRefused:
    found = corrections(view).get(ident)
    if found is None:
        return CommandRefused(CORRECTION_UNKNOWN, f"no correction {ident}")
    if found.state != "proposed":
        return CommandRefused(CORRECTION_CLOSED, f"{ident} is {found.state}")
    if by != found.approver:
        return CommandRefused(NOT_DOMAIN_OWNER, f"{ident} is decided by {found.approver!r}, "
                              f"not {by!r}")  # fmt: skip
    return found


def decide_spec(
    approve: bool,
    *,
    version_now: VersionNow,
    kinds: Mapping[str, ArtifactKind] | None = None,
) -> CommandSpec:
    """The ``decide_correction`` command (module doc, item 2).

    ``version_now`` reads the target's version now; ``kinds`` are the declared artifact
    kinds by full name, which an artifact correction's value is checked against.
    """

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        ident = str(command.args["correction"])
        found = _open(view, ident, str(command.args["by"]))
        if isinstance(found, CommandRefused):
            return found
        decision = "approved" if approve else "rejected"
        payload: dict[str, Any] = {"correction": ident, "interrupt": found.interrupt,
                                   "decision": decision, "by": str(command.args["by"])}  # fmt: skip
        note = command.args.get("note")
        if note:
            payload["note"] = str(note)
        resolved = {"interrupt": found.interrupt, "by": str(command.args["by"]),
                    "decision": "granted" if approve else "rejected"}  # fmt: skip
        events = [NewEvent(DECIDED, payload), NewEvent("interrupt.resolved", resolved)]
        if not approve:
            return CommandAccepted(tuple(events))
        if version_now(found.target) != found.target.version:
            message = f"{found.target} changed since {ident} was proposed; propose again"
            return CommandRefused(TARGET_MOVED, message)
        if found.route == "outbox":
            args = {"correction": ident, "source": found.target.source, "record": found.target.id,
                    "text": found.value, "value_hash": found.value_hash}  # fmt: skip
            spec = EffectSpec(found.system, CORRECTION_EFFECT.key_fields,
                              CORRECTION_EFFECT.semantics,
                              record_fields=CORRECTION_EFFECT.record_fields)  # fmt: skip
            done = enqueue(view, CORRECTION_TOOL, args, spec, command_id=command.command_id)
            if done.event is not None:
                events.append(done.event)
            return CommandAccepted(tuple(events))
        if found.route == "artifact":
            saved = _artifact_version(view, found, kinds or {})
            if isinstance(saved, CommandRefused):
                return saved
            saves, row = saved
            applied = NewEvent(APPLIED, {"correction": ident, "route": "artifact",
                                         "version": str(saves[0].payload["ref"])})  # fmt: skip
            events.extend((*saves, applied))
            return CommandAccepted((*events, *confirmations(view, events, (row,))), (row,))
        return CommandAccepted(tuple(events))  # upload: applied after the file is written

    return CommandSpec("decide_correction", DECISION_BOUNDARY, decide)


def decide_correction(
    store: EventStore,
    owner: str,
    ident: str,
    *,
    approve: bool,
    by: str,
    version_now: VersionNow,
    kinds: Mapping[str, ArtifactKind] | None = None,
    note: str = "",
    command_id: str | None = None,
) -> CommandReceipt:
    """The domain owner approves or rejects ``ident`` (module doc, item 2)."""
    args: dict[str, object] = {"correction": ident, "approve": approve, "note": note}
    command = _command("decide_correction", owner, args, command_id, by)
    return dispatch(store, command, decide_spec(approve, version_now=version_now, kinds=kinds))


def _set_at(content: Any, pointer: str, value: Any) -> Any:
    """``content`` with the JSON pointer ``pointer`` (RFC 6901) replaced by ``value``."""
    if pointer in ("", "/"):
        return value
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.lstrip("/").split("/")]
    root = thaw(content)
    here = root
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if isinstance(here, list):
            index = int(part)
            if not 0 <= index < len(here):
                raise KeyError(pointer)
            if last:
                here[index] = value
            else:
                here = here[index]
        elif isinstance(here, dict):
            if last:
                if part not in here:
                    raise KeyError(pointer)
                here[part] = value
            else:
                here = here[part]
        else:
            raise KeyError(pointer)
    return root


def corrected_content(old: ArtifactVersion, path: str | None, value: Any) -> Any:
    """What an artifact correction saves: ``old``'s content with the value at ``path``."""
    return _set_at(old.content, path or "", thaw(value))


def _artifact_version(
    view: LogView, found: Correction, kinds: Mapping[str, ArtifactKind]
) -> tuple[tuple[NewEvent, ...], ArtifactRow] | CommandRefused:
    """The corrected version of an artifact slot: the old one's reads, plus the old one.

    Its ``artifact.saved`` comes first, then the citations it carries over.
    """
    old_ref = ArtifactRef.parse(f"{found.target.id}@{found.target.version}")
    old = version(view, old_ref)
    kind = kinds.get(old_ref.kind)
    if old is None or kind is None:
        return CommandRefused(TARGET_MOVED, f"{old_ref} or its kind is not loaded")
    try:
        content = corrected_content(old, found.target.path, found.value)
        if not isinstance(content, dict):
            raise SaveRefused("schema", "", "an artifact's content is an object")
        space = old_ref.kind.rpartition(".")[0]
        content = computed(  # a corrected count is recomputed, as at any save
            kind, content, lambda n: current(view, n if "." in n else f"{space}.{n}", None)
        )
        hashed = checked_content(kind, content)
    except (KeyError, ValueError, SaveRefused) as e:
        return CommandRefused(VALUE_INVALID, f"{found.target}: {e}")
    prior = current(view, old_ref.kind, old_ref.key)
    number = (old_ref.version if prior is None else prior.ref.version) + 1
    ref = ArtifactRef(old_ref.kind, old_ref.key, number)
    reads = tuple(sorted({*old.reads, old.ref.as_input()}, key=lambda r: r.sort_key()))
    canonical = canonical_hash(kind, content)
    payload: dict[str, Any] = {
        "ref": str(ref),
        "kind": ref.kind,
        "key": ref.key,
        "version": ref.version,
        "content_hash": hashed,
        "canonical_hash": canonical,
        "reads": [r.as_json() for r in reads],
        "reads_hash": reads_hash(reads),
        "primary_inputs": [old.ref.as_input().as_json()],
        "writer": old.writer.as_json(),
        "origin": CORRECTION_ORIGIN,
        "correction": found.correction,
    }
    row = ArtifactRow(str(ref), ref.kind, ref.key, ref.version, content, hashed, canonical)
    cites = _carried_citations(view, old, str(ref), found.target.path or "")
    return (NewEvent("artifact.saved", payload), *cites), row


def _carried_citations(
    view: LogView, old: ArtifactVersion, carrier: str, path: str
) -> tuple[NewEvent, ...]:
    """The old version's ``citation.bound`` facts outside the corrected value.

    A citation inside the corrected value is dropped: the value it was bound in is gone.
    Every other one names the same content at the same pointer and is carried to the new
    carrier, including an excerpt beside the corrected value in the object that holds it,
    which still names where the old value came from (the correction's own evidence is on
    ``correction.proposed``).
    """
    whole = path in ("", "/")
    out: list[NewEvent] = []
    for e in view.events:
        if e.type != "citation.bound" or e.payload.get("carrier") != str(old.ref):
            continue
        pointer = str(e.payload.get("pointer", ""))
        if whole or pointer == path or pointer.startswith(path + "/"):
            continue
        payload = {**thaw(e.payload), "carrier": carrier, "n": len(out)}
        out.append(NewEvent("citation.bound", payload))
    return tuple(out)


# --- applying an approved source correction -----------------------------------------------


def pending(view: LogView) -> tuple[Correction, ...]:
    """Approved source corrections not yet applied, in proposal order."""
    return tuple(
        c
        for c in corrections(view).values()
        if c.state == "approved" and c.route in ("upload", "outbox")
    )


def _record(
    store: EventStore,
    owner: str,
    found: Correction,
    sources: Iterable[VersionedSource],
    run_query: QueryRunner | None,
    extra: Mapping[str, Any],
) -> bool:
    """What applying ``found`` moved, and ``correction.applied``, in one U8 transaction."""
    held = tuple(sources)
    view = load_view(store, owner)
    events = list(record_source_changes(view, held, cause="command", run_query=run_query,
                                        change_set=change_set(found.correction)))  # fmt: skip
    changed = [e for e in events if e.type == "source.changed"]
    number = (int(changed[0].payload["source_version"]) if changed
              else source_facts(view).source_version)  # fmt: skip
    record_version: str | None = None
    for s in held:
        if s.name == found.target.source:
            record_version = s.record_version(owner, found.target.id)
    payload: dict[str, Any] = {"correction": found.correction, "route": found.route,
                               "source_version": number, "record_version": record_version,
                               **extra}  # fmt: skip
    applied = NewEvent(APPLIED, payload)
    try:
        store.append(owner, SOURCE_CHANGE_BOUNDARY,
                     (*events, *confirmations(view, events), applied))  # fmt: skip
    except AlreadyRecorded:
        return False
    return True


def apply_upload(
    store: EventStore,
    owner: str,
    found: Correction,
    inputs: InputWorkspace,
    sources: Iterable[VersionedSource],
    *,
    run_query: QueryRunner | None = None,
) -> bool:
    """Write an approved ``upload`` correction's text as the file's next version, then record it.

    The file is written only while it still holds the version the proposer saw: an upload
    made after the decision and before this pass (a process that stopped in between) is
    never overwritten, and the correction stays approved and unapplied. A file that already
    holds the corrected text is not written again; only its record is.
    """
    held = inputs.reader(owner).read(inputs.name, found.target.id)
    text = str(found.value)
    if held is None or held.text != text:
        if held is None or held.version != found.target.version:
            return False
        _, _, path = found.target.id.partition("/")
        inputs.put(owner, {path: text.encode("utf-8")})
    return _record(store, owner, found, sources, run_query, {})


def apply_outbox(
    store: EventStore,
    owner: str,
    found: Correction,
    sources: Iterable[VersionedSource],
    *,
    drain: Callable[[], object],
    run_query: QueryRunner | None = None,
) -> bool:
    """Deliver an approved ``outbox`` correction, then record what its write moved.

    ``drain`` runs the owner's outbox worker. Nothing is recorded until the entry has
    completed; an abandoned entry leaves the correction ``failed``.
    """
    entry = _entry(load_view(store, owner), found.correction)
    if entry is None:
        return False
    if entry.state == "enqueued":
        drain()
        entry = _entry(load_view(store, owner), found.correction)
    if entry is None or entry.state != "completed":
        return False
    return _record(store, owner, found, sources, run_query, {"outbox_key": entry.key})


def _entry(view: LogView, ident: str) -> Any:
    found = (e for e in entries(view).values() if e.tool == CORRECTION_TOOL)
    return next((e for e in found if e.request.get("correction") == ident), None)
