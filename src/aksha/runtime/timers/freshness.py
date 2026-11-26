"""Freshness lifetimes become facts (ADR-P25 D3). Pure: builds timers, never appends.

A versioned source may declare ``max_age_s`` (RFC 9111's freshness lifetime) for the
records of its collections, and an artifact kind may declare one for its versions. The
agent loop asks :func:`recheck_timers` with each step's recorded reads and saves, and
commits the ``recheck`` timers it returns in that step's result (U2):

1. A record read from a collection with a lifetime schedules a recheck ``after_ms`` the
   lifetime from the step's commit, unless a recheck of that record is already pending:
   the oldest fetch expires first, and the expiry covers every version saved before it.
2. A saved version of a kind with a lifetime schedules a recheck of that version.

When a recheck fires, ``record_source_changes`` writes ``source.expired`` for its target
and ordinary invalidation follows (``firing.py``). Timer IDs are counted from the log
(``recheck:<kind>:<id>:<n>``), so a replayed step names the same timer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from aksha.runtime.artifacts.types import ArtifactKind, ArtifactRef, InputRef, WriterId
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.invalidation.types import VersionedSourceSpec
from aksha.runtime.projections.view import LogView
from aksha.runtime.stop_policy.types import AgentScope
from aksha.runtime.timers.facts import scheduled
from aksha.runtime.timers.types import TimerSet
from aksha.runtime.timers.view import timers


def lifetimes(specs: Iterable[VersionedSourceSpec]) -> Mapping[str, int]:
    """Each collection of a source that declares ``max_age_s``, to that lifetime in seconds."""
    out: dict[str, int] = {}
    for spec in specs:
        if spec.max_age_s is not None:
            for collection in spec.collections:
                out.setdefault(collection, spec.max_age_s)
    return MappingProxyType(out)


def _key(target: InputRef) -> str:
    return f"recheck:{target.kind}:{target.id}" + (
        f"@{target.version}" if target.kind == "artifact" else ""
    )


def recheck_timers(
    view: LogView,
    reads: Iterable[InputRef],
    saved: Iterable[ArtifactRef],
    life: Mapping[str, int],
    kinds: Mapping[str, ArtifactKind],
    writer: WriterId,
    *,
    run: str | None = None,
    instance: str | None = None,
    step: str | None = None,
) -> tuple[NewEvent, ...]:
    """The recheck timers a step's ``reads`` and ``saved`` versions call for (module doc)."""
    known = timers(view)
    counts: dict[str, int] = {}
    open_keys: set[str] = set()
    for t in known.values():
        if t.spec.cause == "recheck" and t.spec.target is not None:
            key = _key(t.spec.target)
            counts[key] = counts.get(key, 0) + 1
            if t.pending:
                open_keys.add(key)
    wanted: list[tuple[InputRef, int]] = []
    for read in reads:
        if read.kind == "record":
            seconds = life.get(read.id.split("/", 1)[0])
            if seconds is not None:
                wanted.append((read, seconds))
    for ref in saved:
        kind = kinds.get(ref.kind)
        if kind is not None and kind.max_age_s is not None:
            wanted.append((ref.as_input(), kind.max_age_s))
    out: list[NewEvent] = []
    for target, seconds in wanted:
        key = _key(target)
        if key in open_keys:
            continue
        open_keys.add(key)
        counts[key] = counts.get(key, 0) + 1
        timer = TimerSet(f"{key}:{counts[key]}", AgentScope(writer.agent), "recheck",
                         after_ms=seconds * 1000, target=target)  # fmt: skip
        out.append(scheduled(timer, run=run, instance=instance, step=step))
    return tuple(out)
