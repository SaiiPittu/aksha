"""Whether a run is finalizing: its ``run.finalizing`` fact, read from the event store (D4).

A reducer over one fact type, so the Task statuses (``tasks.status``) can read it without
importing anything above the event store. A run is finalizing from its first
``run.finalizing`` on; nothing ends that state but the run's end.
"""

from __future__ import annotations

from types import MappingProxyType

from aksha.runtime.finalization.types import Finalizing
from aksha.runtime.projections.view import LogView

RUN_FINALIZING = "run.finalizing"


def finalizing(view: LogView, run: str) -> Finalizing | None:
    """The run's first ``run.finalizing`` fact, or None while it is not finalizing. Pure."""
    for e in view.events:
        if e.run != run or e.type != RUN_FINALIZING:
            continue
        p = e.payload
        reserve = {str(k): int(v) for k, v in dict(p.get("reserve") or {}).items()}
        return Finalizing(
            run=run,
            interrupt=str(p.get("interrupt", "")),
            by=str(p.get("by", "")),
            reserve=MappingProxyType(reserve),
            finalizers=tuple(str(f) for f in p.get("finalizers") or ()),
            voided=tuple(str(v) for v in p.get("voided") or ()),
            seq=e.seq,
        )
    return None
