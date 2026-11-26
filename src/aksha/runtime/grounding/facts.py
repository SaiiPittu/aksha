"""Grounding facts and the evidence a save is verified against (D8, decision 6).

``citation.bound`` records each verified citation of a saved version, in the save's
transaction (U2); its dedup key is ``cite:{carrier}#{n}``. :func:`evidence` builds the
:class:`~aksha.runtime.grounding.verify.Evidence` for one Task instance from the owner's
log: the span index, the record versions the log last recorded (record grain, S2), the
steps that failed, and, for an instance that resumed an earlier session (decision 36), the
spans shown inside that session's turns: those count as shown to this instance too, because
the resumed context shows them again. A stale one is still refused (``stale_source``). All
pure.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from aksha.runtime.artifacts.types import ArtifactRef, InputRef
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.errors import StepIdInvalid
from aksha.runtime.execution.types import StepId
from aksha.runtime.grounding.spans import span_index
from aksha.runtime.grounding.types import Verified
from aksha.runtime.grounding.verify import Evidence
from aksha.runtime.invalidation.sources import source_facts
from aksha.runtime.projections.view import LogView


def citation_bound(
    verified: Verified, *, carrier: str, n: int, run: str, instance: str, step: str
) -> NewEvent:
    """The ``citation.bound`` fact for the ``n``-th verified citation of ``carrier``."""
    payload: dict[str, Any] = {
        "carrier": carrier,
        "n": n,
        "pointer": verified.pointer,
        "form": verified.form,
        "target": verified.target,
    }
    if verified.quote is not None:
        payload["quote"] = verified.quote
        payload["occurrence"] = verified.occurrence
    if verified.path is not None:
        payload["path"] = verified.path
    return NewEvent("citation.bound", payload, run=run, instance=instance, step=step)


def failed_steps(view: LogView) -> frozenset[str]:
    """Every step the log records as failed: an attempt failed or a result not ``ok``."""
    out: set[str] = set()
    for e in view.events:
        failed = e.type == "step.failed" or (
            e.type == "step.completed" and e.payload.get("outcome") != "ok"
        )
        if failed and e.step is not None:
            out.add(e.step)
    return frozenset(out)


def _whole(read: InputRef) -> InputRef:
    return read if read.path is None else InputRef(read.kind, read.id, read.version)


def resumed_spans(view: LogView, run: str, instance: str) -> frozenset[str]:
    """The span IDs shown inside the session ``instance`` resumed in ``run`` (decision 36).

    The session is the segments its latest ``task.entered{reentry}`` in ``run`` names, when
    that entry chose ``resume``; otherwise there is none. A span counts only when it was
    shown to a segment's instance within the segment's turns (from the first event of turn
    ``from_turn`` to the last of ``through_turn``, so an answer shown between two of its
    turns counts too); one shown in a turn the resumed context leaves out does not.
    """
    latest: Mapping[str, Any] | None = None
    for e in view.events:
        if e.type == "task.entered" and e.run == run and e.instance == instance:
            latest = e.payload
    choice = latest.get("reentry") if latest is not None else None
    if not isinstance(choice, Mapping) or choice.get("mode") != "resume":
        return frozenset()
    out: set[str] = set()
    for seg in choice.get("session", ()):
        if not isinstance(seg, Mapping):
            continue
        srun, sinst = str(seg["run"]), str(seg["instance"])
        lo, hi = int(seg["from_turn"]), int(seg["through_turn"])
        seqs = [e.seq for e in view.events if _turn_of(e.step, srun, sinst) in range(lo, hi + 1)]
        if not seqs:
            continue
        first, last = min(seqs), max(seqs)
        for e in view.events:
            if e.run != srun or e.instance != sinst or not first <= e.seq <= last:
                continue
            if e.type == "excerpts.minted":
                out.update(str(i) for i in e.payload.get("shown", ()))
            elif e.type == "excerpts.shown":
                out.update(str(i) for i in e.payload["spans"])
    return frozenset(out)


def _turn_of(step: str | None, run: str, instance: str) -> int | None:
    """The turn number of ``step`` when it is a step of ``instance`` in ``run``, else None."""
    if step is None:
        return None
    try:
        sid = StepId.parse(step)
    except StepIdInvalid:
        return None
    return sid.turn if sid.run == run and sid.instance == instance else None


def evidence(
    view: LogView,
    *,
    run: str,
    instance: str,
    reads: Iterable[InputRef],
    artifact: Callable[[ArtifactRef], Mapping[str, Any] | None],
) -> Evidence:
    """What a save by ``instance`` in ``run`` is verified against, read from ``view``."""
    return Evidence(
        spans=span_index(view),
        run=run,
        instance=instance,
        records=source_facts(view).records,
        reads=frozenset(str(_whole(r)) for r in reads),
        artifact=artifact,
        failed_steps=failed_steps(view),
        resumed_spans=resumed_spans(view, run, instance),
    )
