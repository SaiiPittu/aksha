"""Cross-run reuse (ADR-P04 R1): a later run takes an earlier run's versions by writer key.

A Task instance writes the slots its writer key (agent, Task path, key) names, and the key
is the same in every run of the agent for one owner. When a later request run enters an
instance whose writer already has current versions written for another run, the runtime
decides at entry, in the stop decision that records ``task.entered`` (U3):

1. Reuse. Every such version is reusable: its origin is a Task's save, a derived kind or
   an approved correction (never a person's choice, which was made for its own run);
   its run started under the same agent definition hash and the same request text; it is
   not invalidated or dirty by the view's rules, whatever its run's invalidation mode;
   and every recorded read still holds for the new run: a record or query at the
   version last recorded for it, a statement not superseded, and an artifact read whose
   slot holds no newer version written for the new run or for the version's own run,
   or only an equal one (early cutoff). One ``artifact.reused`` fact per version is
   recorded with the entry, and the entry's completion check sees them, so the instance
   pops DONE with no model turn.
2. Recompute. Otherwise nothing is recorded, and the instance's own completion check does
   not see those versions (:func:`hidden_for`): it starts work and saves its own. A
   version an approved correction saved is never hidden: it replaces the slot for every
   run, so a run that recomputes does not regenerate over the domain owner's value.

The request is compared whole because it is a dependency no version records: every turn
of the run that wrote it showed the model that text. Reads are compared as recorded:
every recorded read is a dependency here; where a version's citations narrow its strong
dependencies, the same check applies to those only.

``run.started{reuse: "off"}`` turns it off for a run (``Agent.reuse`` or the runtime's
``AKSHA_REUSE=off``): every instance of the run recomputes. A run that records nothing
reuses. A rebuild run never reuses: the rebuild plan decides what it re-enters. Pure: the
functions read a ``LogView`` and return facts for the caller to commit.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

from aksha.runtime.artifacts.types import ArtifactVersion, WriterId
from aksha.runtime.artifacts.versions import current
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.invalidation.types import STALE_STATES
from aksha.runtime.invalidation.view import ReadChecker, runs_for, withdrawn_writers
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.rebuilds import is_rebuild_run

ReuseSetting = Literal["on", "off"]
"""Whether a run may reuse an earlier run's versions (module doc)."""

REUSE_SETTINGS: tuple[ReuseSetting, ...] = ("on", "off")
REUSE_FIELD = "reuse"
"""The ``run.started`` field that records the setting; a run that records none reuses."""

ARTIFACT_REUSED = "artifact.reused"
"""The fact one reuse records, with the entry (U3)."""

REUSABLE_ORIGINS = frozenset({"task", "derived", "correction"})
"""Origins a later run may reuse: never a person's choice, made for its own run."""


def reuse_setting(view: LogView, run: str) -> ReuseSetting:
    """What ``run.started{reuse}`` recorded for ``run``; ``on`` when it recorded nothing."""
    for e in view.events:
        if e.type == "run.started" and e.run == run:
            return "off" if e.payload.get(REUSE_FIELD) == "off" else "on"
    return "on"


def run_request(view: LogView, run: str) -> str:
    """The text of the message that opened ``run`` (empty when none did).

    A reminder's run states its request on its ``run.started``. A rebuild run no message
    opened has the request of the first run it rebuilds for (``for_runs``).
    """
    for e in view.events:
        if e.type == "run.started" and e.run == run and e.payload.get("request"):
            return str(e.payload["request"])
    message: str | None = None
    for e in view.events:
        if e.type == "route.decided" and e.payload.get("run") == run:
            message = str(e.payload.get("message"))
            break
    if message is None:
        for e in view.events:
            if e.type == "run.started" and e.run == run:
                for_runs = [str(r) for r in e.payload.get("for_runs", ()) if r != run]
                return run_request(view, for_runs[0]) if for_runs else ""
        return ""
    for e in view.events:
        if e.type == "user.message" and e.payload.get("message") == message:
            return str(e.payload.get("text", ""))
    return ""


def reused_into(view: LogView, run: str) -> frozenset[str]:
    """The versions ``run`` recorded ``artifact.reused`` for."""
    return frozenset(
        str(e.payload["version"]) for e in view.events if e.type == ARTIFACT_REUSED and e.run == run
    )


def reuses(view: LogView) -> Mapping[str, tuple[str, ...]]:
    """Each run to the versions it reused, in record order (for status and the trace)."""
    out: dict[str, list[str]] = {}
    for e in view.events:
        if e.type == ARTIFACT_REUSED and e.run is not None:
            out.setdefault(e.run, []).append(str(e.payload["version"]))
    return MappingProxyType({r: tuple(v) for r, v in out.items()})


def _writer_refs(view: LogView, writer: WriterId) -> tuple[tuple[str, str, str | None], ...]:
    """Every version the writer saved, as (ref, kind, key), in save order."""
    mine = writer.as_json()
    return tuple(
        (str(e.payload["ref"]), str(e.payload["kind"]), e.payload.get("key"))
        for e in view.events
        if e.type == "artifact.saved" and e.payload.get("writer") == mine
    )


def _visible(
    ref: str, run_of: str | None, line: frozenset[str], lines: Mapping[str, frozenset[str]],
    reused: frozenset[str],
) -> bool:  # fmt: skip
    if ref in reused:
        return True
    if run_of is None:
        return False
    return bool(lines.get(run_of, frozenset({run_of})) & line)


def hidden_for(view: LogView, run: str, writer: WriterId) -> frozenset[str]:
    """The writer's versions ``run``'s completion checks must not see (module doc, item 2).

    Every version the writer saved for another run's line that ``run`` did not reuse,
    except a correction's version, which counts for every run. A rebuild run hides
    nothing: the rebuild plan governs what it sees as done.
    """
    refs = _writer_refs(view, writer)
    if not refs or is_rebuild_run(view, run):
        return frozenset()
    lines = runs_for(view)
    line = lines.get(run, frozenset({run}))
    reused = reused_into(view, run) | _corrections(view)
    runs = {str(e.payload["ref"]): e.run for e in view.events if e.type == "artifact.saved"}
    return frozenset(r for r, _, _ in refs if not _visible(r, runs.get(r), line, lines, reused))


def _corrections(view: LogView) -> frozenset[str]:
    """Versions an approved correction saved: never hidden, whatever the run's setting.

    A correction replaces the slot for every reader (ADR-P05 C1e), so a later run, even
    one that recomputes, sees the domain owner's value as done instead of regenerating
    over it.
    """
    return frozenset(
        str(e.payload["ref"])
        for e in view.events
        if e.type == "artifact.saved" and e.payload.get("origin") == "correction"
    )


def _started(view: LogView) -> Mapping[str, Mapping[str, object]]:
    return MappingProxyType(
        {str(e.run): e.payload for e in view.events if e.type == "run.started" and e.run}
    )


def candidates(view: LogView, run: str, writer: WriterId) -> tuple[ArtifactVersion, ...]:
    """The writer's current versions written for another line that ``run`` has not reused."""
    hidden = hidden_for(view, run, writer)
    if not hidden:
        return ()
    slots = dict.fromkeys((kind, key) for _, kind, key in _writer_refs(view, writer))
    out: list[ArtifactVersion] = []
    for kind, key in slots:
        now = current(view, kind, key)
        if now is not None and now.writer == writer and str(now.ref) in hidden:
            out.append(now)
    return tuple(out)


def refusal(view: LogView, run: str, v: ArtifactVersion, checker: ReadChecker) -> str | None:
    """Why ``run`` may not reuse ``v`` (module doc, item 1), or None when it may."""
    if v.origin not in REUSABLE_ORIGINS:
        return "origin"
    if v.writer in withdrawn_writers(view):
        return "withdrawn"
    started = _started(view)
    if v.run is not None:
        then, now = started.get(v.run, {}), started.get(run, {})
        if then.get("definition_hash") != now.get("definition_hash"):
            return "definition_changed"
        if run_request(view, v.run) != run_request(view, run):
            return "request_changed"
    if checker.freshness(str(v.ref)) in STALE_STATES:
        return "stale"
    for status in checker.holds(str(v.ref), checker.line(run)):
        if status.state not in ("same", "equivalent"):
            return f"read_{status.state}"
    return None


def reuse_facts(view: LogView, run: str, writer: WriterId, instance: str) -> tuple[NewEvent, ...]:
    """The ``artifact.reused`` facts entering ``instance`` of ``run`` records, or none.

    All or nothing: the writer's versions are one unit of work, so one version that may not
    be reused means the instance recomputes all of them.
    """
    if reuse_setting(view, run) == "off" or is_rebuild_run(view, run):
        return ()
    found = candidates(view, run, writer)
    if not found:
        return ()
    checker = ReadChecker(view)
    if any(refusal(view, run, v, checker) is not None for v in found):
        return ()
    return tuple(
        NewEvent(
            ARTIFACT_REUSED,
            {"version": str(v.ref), "from_run": v.run, "writer": writer.as_json()},
            run=run,
            instance=instance,
        )
        for v in found
    )
