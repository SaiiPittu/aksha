"""Resolving a proposed correction's target against the live sources and artifacts.

The session calls :func:`resolve` before it dispatches ``propose_correction``: the target
text names a record of one versioned source (by its collection) or an artifact slot of a
loaded kind, and the result fixes the version the proposer saw, what the target holds
now (so an equal value is refused), the route an approval will take and the domain
owner's role. :func:`version_now` reads the same version again when the owner decides.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

from aksha.runtime.artifacts.types import ArtifactRef
from aksha.runtime.artifacts.versions import current
from aksha.runtime.canonical import thaw
from aksha.runtime.corrections.types import (
    NO_SYSTEM_OF_RECORD,
    TARGET_MOVED,
    TARGET_UNKNOWN,
    CorrectionRouting,
    Route,
    Target,
)
from aksha.runtime.grounding.ports import RecordReader
from aksha.runtime.invalidation.ports import InputWorkspace, VersionedSource
from aksha.runtime.projections.view import LogView


@dataclass(frozen=True)
class Resolved:
    """A target the owner's sources or artifacts hold now, and where its correction goes."""

    target: Target
    held: Any
    route: Route
    approver: str
    system: str | None = None


@dataclass(frozen=True)
class Unresolved:
    """Why a target cannot be corrected: a refusal code and a message."""

    code: str
    message: str


def _pointer_value(content: Any, pointer: str) -> Any:
    here = thaw(content)
    if pointer in ("", "/"):
        return here
    for part in pointer.lstrip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(here, list):
            here = here[int(part)]
        elif isinstance(here, dict):
            here = here[part]
        else:
            raise KeyError(pointer)
    return here


def resolve(
    text: str,
    owner: str,
    view: LogView,
    *,
    sources: Sequence[VersionedSource],
    reader: RecordReader | None,
    inputs: InputWorkspace | None,
    kinds: Collection[str],
    routing: CorrectionRouting,
    systems: Collection[str],
) -> Resolved | Unresolved:
    """The target ``text`` names (module doc)."""
    base, hash_, pointer = text.partition("#")
    head = base.split("@", 1)[0].split("/", 1)[0]
    if head in kinds:
        return _artifact(base, pointer if hash_ else None, view, routing)
    if hash_:
        return Unresolved(TARGET_UNKNOWN, f"{text}: a record is corrected whole, with no pointer")
    collection = base.split("/", 1)[0]
    source = next((s for s in sources if collection in s.collections), None)
    if source is None:
        return Unresolved(TARGET_UNKNOWN, f"no source serves {base!r} and no kind is {head!r}")
    version = source.record_version(owner, base)
    if version is None:
        return Unresolved(TARGET_UNKNOWN, f"{base} is not in source {source.name!r}")
    found = None if reader is None else reader.read(source.name, base)
    held = None if found is None else found.text
    target = Target("record", base, version, source=source.name)
    approver = routing.approver_for(source.name)
    if inputs is not None and source.name == inputs.name and inputs.uploaded(owner, base):
        return Resolved(target, held, "upload", approver)
    system = routing.systems.get(source.name)
    if system is None or system not in systems:
        return Unresolved(NO_SYSTEM_OF_RECORD, f"{base} is not an upload of yours and source "
                          f"{source.name!r} names no system of record for corrections")  # fmt: skip
    return Resolved(target, held, "outbox", approver, system)


def _artifact(
    base: str, pointer: str | None, view: LogView, routing: CorrectionRouting
) -> Resolved | Unresolved:
    named, at, _ = base.partition("@")
    kind, _, key = named.partition("/")
    now = current(view, kind, key or None)
    if now is None:
        return Unresolved(TARGET_UNKNOWN, f"no version of {named} is saved")
    if at and str(now.ref) != base:
        return Unresolved(TARGET_MOVED, f"{base} is not current; the current version is "
                          f"{now.ref}")  # fmt: skip
    try:
        held = _pointer_value(now.content, pointer or "")
    except (KeyError, IndexError, ValueError):
        return Unresolved(TARGET_UNKNOWN, f"{now.ref} has nothing at {pointer!r}")
    target = Target("artifact", now.ref.slot_id(), f"v{now.ref.version}", path=pointer)
    return Resolved(target, held, "artifact", routing.approver_for(kind.split(".", 1)[0]))


def version_now(
    target: Target, owner: str, view: LogView, sources: Sequence[VersionedSource]
) -> str | None:
    """``target``'s version now: a record's from its source, an artifact slot's current one."""
    if target.kind == "artifact":
        ref = ArtifactRef.parse(f"{target.id}@{target.version}")
        now = current(view, ref.kind, ref.key)
        return None if now is None else f"v{now.ref.version}"
    for s in sources:
        if s.name == target.source:
            return s.record_version(owner, target.id)
    return None
