"""The patent agent's elapsed days depend on a recorded date (ADR-P25 D4, B1, B2).

Its tools count a disclosure's elapsed days to ``ToolContext.run_date``. Reading that date
is a recorded ``time:clock`` read, so the extraction built on one day is invalidated once
the runtime records the next day, and nothing built from a source alone is held by it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from aksha.runtime.invalidation.confirm import confirmations
from aksha.runtime.invalidation.view import invalidation_view
from aksha.runtime.timers.clock import date_change, time_read
from tests.support.patent import OWNER, PatentRig


def test_a_new_day_invalidates_the_extraction_that_counted_days_to_the_old_one(
    tmp_path: Path,
) -> None:
    rig = PatentRig(tmp_path / "aksha.sqlite")
    rig.run_to_selection()
    ext = rig.current("extraction", "C1")
    assert ext is not None
    assert time_read("2026-11-26") in ext.reads
    assert invalidation_view(rig.view()).state(ext.ref).freshness == "current"
    rig.clock.advance(86_400)
    view = rig.view()
    events = date_change(view, date(2026, 11, 27))
    assert events, "the run open since yesterday makes the new day matter"
    rig.store.append(OWNER, "U8", (*events, *confirmations(view, events)))
    state = invalidation_view(rig.view()).state(ext.ref)
    assert state.freshness == "invalidated"
    assert [str(r.input) for r in state.changed_reads()] == ["time:clock@2026-11-26"]
    rig.close()


def test_the_same_day_records_nothing_and_invalidates_nothing(tmp_path: Path) -> None:
    rig = PatentRig(tmp_path / "aksha.sqlite")
    rig.run_to_selection()
    assert date_change(rig.view(), date(2026, 11, 26)) == ()
    assert invalidation_view(rig.view()).refs("invalidated") == ()
    rig.close()
