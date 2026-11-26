"""The current date as a recorded input (ADR-P25 D4): a read, and a versioned source.

1. A read. A tool body that needs the date calls :func:`observe_time`, which returns
   ``InputRef(kind="time", id="clock", version=<ISO date>)`` and records it in the
   attempt's ``TimeReads``; the agent loop records every time read as one of the
   instance's reads with the step's result. Amendment B2: reading
   ``ToolContext.run_date`` is the same recorded read, so a body written against
   amendment B1 gets D4's invalidation without a change.
2. The date a body sees. :func:`recorded_date` is the later of the run's recorded
   ``run.started`` date and the last date the runtime recorded for the owner, both read
   from the log as of the step, so a replay sees the date the first attempt saw and a run
   open across midnight counts to the new day once that day is recorded.
3. The date as a source. :func:`date_change` records the injected clock's date as the
   ``time`` source's version, through the one writer of source facts
   (``record_source_changes``, cause ``clock``). A ``time:clock`` read of another date is
   then ``moved`` in the invalidation view, before any reuse decision, whether or not the
   later run reads the date again. It records only when the date can matter: an open run
   started on an earlier day, or a recorded read of an earlier date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.errors import AkshaError
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.invalidation.sources import read_inputs, record_source_changes, source_facts
from aksha.runtime.invalidation.types import CLOCK_RECORD, CLOCK_SOURCE
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS, open_runs
from aksha.runtime.projections.view import LogView
from aksha.runtime.timers.reads import CLOCK_ID
from aksha.runtime.tools.types import ToolContext

__all__ = [
    "CLOCK_RECORD",
    "CLOCK_SOURCE",
    "ClockSource",
    "NoRecordedDate",
    "date_change",
    "observe_time",
    "recorded_date",
    "time_read",
]


class NoRecordedDate(AkshaError):
    """``observe_time`` in a context that carries no recorded date (no run started yet)."""

    default_code = "no_recorded_date"


def time_read(iso_date: str) -> InputRef:
    """The lineage input of a read of ``iso_date``: ``time:clock@<date>``."""
    return InputRef("time", CLOCK_ID, iso_date)


def observe_time(ctx: ToolContext) -> InputRef:
    """The recorded date as a lineage input, recorded as this attempt's read (module doc, 1).

    Raises :class:`NoRecordedDate` when the context has no date.
    """
    today = ctx.run_date  # reading it records it (B2)
    if today is None:
        raise NoRecordedDate(message="this call's context carries no recorded date")
    return time_read(today)


def recorded_date(view: LogView, run: str) -> str | None:
    """The date a body of ``run`` counts to (module doc, 2); None before the run started."""
    started = next((e for e in view.events if e.type == "run.started" and e.run == run), None)
    if started is None:
        return None
    day = started.at[:10]
    last = source_facts(view).records.get(CLOCK_RECORD)
    return max(day, last) if last is not None else day


@dataclass(frozen=True)
class ClockSource:
    """The ``time`` source on one date: a ``VersionedSource`` whose version is the date."""

    today: date
    name: str = CLOCK_SOURCE
    collections: tuple[str, ...] = (CLOCK_SOURCE,)

    def version(self, owner: str) -> str:
        """The date, for every owner."""
        return self.today.isoformat()

    def record_version(self, owner: str, record_id: str) -> str | None:
        """The date is the one record's version."""
        return self.today.isoformat() if record_id == CLOCK_RECORD else None


def date_change(view: LogView, today: date) -> tuple[NewEvent, ...]:
    """The ``source.changed{cause: clock}`` that records ``today``, or nothing (module doc, 3).

    Pure: the caller commits what it returns, with what it confirms, in U8.
    """
    day = today.isoformat()
    if source_facts(view).records.get(CLOCK_RECORD) == day:
        return ()
    older_read = any(v < day for v in read_inputs(view, "time").values())
    runs = rebuild(RUNS, view)
    starts = {e.run: e.at[:10] for e in view.events if e.type == "run.started" and e.run}
    older_run = any(starts.get(r, day) < day for r in open_runs(runs))
    if not (older_read or older_run):
        return ()
    return record_source_changes(view, (ClockSource(today),), cause="clock")
