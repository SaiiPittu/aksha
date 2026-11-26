"""Grounding facts and the evidence a save is verified against (D8, decision 6).

``citation.bound`` records each verified citation of a saved version, in the save's
transaction (U2); its dedup key is ``cite:{carrier}#{n}``. :func:`evidence` builds the
:class:`~aksha.runtime.grounding.verify.Evidence` for one Task instance from the owner's
log: the span index, the record versions the log last recorded (record grain, S2) and the
steps that failed. All pure.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from aksha.runtime.artifacts.types import ArtifactRef, InputRef
from aksha.runtime.events.envelope import NewEvent
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
    )
