"""Time as a recorded input (ADR-P25 D4): the date read, the clock source, day boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.invalidation.plan import may_invalidate
from aksha.runtime.invalidation.sources import source_facts
from aksha.runtime.timers.clock import (
    CLOCK_RECORD,
    NoRecordedDate,
    date_change,
    observe_time,
    recorded_date,
    time_read,
)
from aksha.runtime.timers.reads import TimeReads
from aksha.runtime.tools.types import ToolContext
from tests.support.invalidation import OWNER, Bench, record

DAY1 = "2026-11-27"
DAY2 = "2026-11-28"


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    b = Bench(tmp_path)
    yield b
    b.close()


def _ctx(run_date: str | None = DAY1, reads: TimeReads | None = None) -> ToolContext:
    return ToolContext("o", "a", "t", None, "s", 1, None, run_date, reads)


def _record(b: Bench, today: str) -> tuple[NewEvent, ...]:
    events = date_change(b.view(), date.fromisoformat(today))
    if events:
        b.store.append(OWNER, "U8", events)
    return events


def test_observe_time_returns_the_date_as_a_lineage_input_and_records_it() -> None:
    reads = TimeReads()
    got = observe_time(_ctx(reads=reads))
    assert got == InputRef("time", "clock", DAY1) == time_read(DAY1)
    assert reads.reads == (got,)


def test_reading_the_run_date_is_a_recorded_read_once_per_attempt() -> None:
    reads = TimeReads()
    ctx = _ctx(reads=reads)
    assert (ctx.run_date, ctx.run_date) == (DAY1, DAY1)
    assert reads.reads == (time_read(DAY1),)


def test_a_context_without_a_collector_or_a_date_records_nothing() -> None:
    assert _ctx().run_date == DAY1
    reads = TimeReads()
    assert _ctx(None, reads).run_date is None
    assert reads.reads == ()
    ctx = _ctx(reads=reads)
    assert ctx.owner == "o" and reads.reads == ()


def test_observe_time_without_a_recorded_date_is_refused() -> None:
    with pytest.raises(NoRecordedDate):
        observe_time(_ctx(None, TimeReads()))


def test_the_recorded_date_is_the_runs_start_then_each_recorded_day(bench: Bench) -> None:
    assert recorded_date(bench.view(), "R-0001") is None
    bench.start("R-0001")
    assert recorded_date(bench.view(), "R-0001") == DAY1
    bench.clock.advance(86_400)
    _record(bench, DAY2)
    assert recorded_date(bench.view(), "R-0001") == DAY2


def test_an_older_recorded_day_never_moves_a_later_runs_date_back(bench: Bench) -> None:
    bench.start("R-0001")
    bench.clock.advance(86_400)
    assert _record(bench, DAY2)
    bench.end("R-0001")
    bench.clock.advance(2 * 86_400)
    bench.start("R-0002")
    assert recorded_date(bench.view(), "R-0002") == "2026-11-30"


def test_nothing_is_recorded_for_an_owner_with_no_older_date_to_move(bench: Bench) -> None:
    """No open run from an earlier day and no read of an earlier date: nothing can move."""
    assert _record(bench, DAY1) == ()
    bench.start("R-0001")
    assert _record(bench, DAY1) == ()
    bench.save("R-0001", "p.note/N", {"days": 3}, reads=[time_read(DAY1)])
    assert _record(bench, DAY1) == ()


def test_a_run_open_across_a_day_boundary_records_the_new_day(bench: Bench) -> None:
    bench.start("R-0001")
    (changed,) = _record(bench, DAY2)
    assert changed.payload["cause"] == "clock"
    assert changed.payload["sources"] == {"time": DAY2}
    assert changed.payload["records"] == {CLOCK_RECORD: DAY2}
    assert _record(bench, DAY2) == ()
    (moved,) = _record(bench, "2026-11-29")
    assert moved.payload["records"] == {CLOCK_RECORD: "2026-11-29"}
    assert source_facts(bench.view()).records[CLOCK_RECORD] == "2026-11-29"


def test_a_later_run_start_records_the_day_when_an_older_date_was_read(bench: Bench) -> None:
    bench.start("R-0001")
    bench.save("R-0001", "p.note/N", {"days": 3}, reads=[time_read(DAY1)])
    bench.end("R-0001")
    (changed,) = _record(bench, DAY2)
    assert changed.payload["records"] == {CLOCK_RECORD: DAY2}


def test_a_day_boundary_invalidates_what_read_the_older_date(bench: Bench) -> None:
    bench.start("R-0001")
    bench.save("R-0001", "p.note/N", {"days": 3}, reads=[time_read(DAY1)])
    bench.save("R-0001", "p.extract/X", {"v": 1}, reads=[record("K")])
    assert bench.iv().state("p.note/N@v1").freshness == "current"
    _record(bench, DAY2)
    view = bench.iv()
    note = view.state("p.note/N@v1")
    assert note.freshness == "invalidated"
    assert [(r.state, r.now) for r in note.changed_reads()] == [("moved", DAY2)]
    assert view.state("p.extract/X@v1").freshness == "current"
    assert may_invalidate(bench.view())


def test_a_rebuild_that_read_the_new_date_is_current(bench: Bench) -> None:
    bench.start("R-0001")
    bench.save("R-0001", "p.note/N", {"days": 3}, reads=[time_read(DAY1)])
    _record(bench, DAY2)
    bench.save("R-0001", "p.note/N", {"days": 4}, reads=[time_read(DAY2)], instance="again")
    assert bench.iv().state("p.note/N@v2").freshness == "current"


def test_a_date_read_before_any_clock_record_is_still_invalidated_next_day(bench: Bench) -> None:
    bench.start("R-0001")
    bench.save("R-0001", "p.note/N", {"days": 3}, reads=[time_read(DAY1)])
    _record(bench, DAY2)
    assert bench.iv().state("p.note/N@v1").freshness == "invalidated"
