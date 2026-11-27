"""ADR-P19 D5: a session's stream admits the rebuild runs opened for the session's runs."""

from __future__ import annotations

from typing import Any

from aksha.runtime.events.envelope import Event
from aksha.runtime.session.streams import SessionFilter

SESSION = "S-0001"


def _event(seq: int, type_: str, *, run: str | None = None, session: str | None = None,
           payload: dict[str, Any] | None = None) -> Event:  # fmt: skip
    return Event(type_, payload or {}, run=run, session=session, seq=seq, owner="o1",
                 at="2026-11-27T09:00:00Z", boot=1, schema_version=1)  # fmt: skip


def test_a_rebuild_run_for_one_of_the_sessions_runs_is_admitted_with_its_events() -> None:
    seen = SessionFilter(SESSION)
    assert seen.admits(_event(1, "run.started", run="R-0001", session=SESSION))
    rebuild = _event(2, "run.started", run="R-0002",
                     payload={"cause": "rebuild", "for_runs": ["R-0001"]})  # fmt: skip
    assert seen.admits(rebuild)
    assert seen.admits(_event(3, "interrupt.raised", run="R-0002"))
    # A rebuild of a rebuild (an accepted review reopens work written for R-0002) follows too.
    assert seen.admits(_event(4, "run.started", run="R-0003",
                              payload={"cause": "rebuild", "for_runs": ["R-0002"]}))  # fmt: skip
    assert seen.admits(_event(5, "run.ended", run="R-0003"))


def test_a_rebuild_for_another_sessions_run_stays_out() -> None:
    seen = SessionFilter(SESSION)
    seen.admits(_event(1, "run.started", run="R-0001", session=SESSION))
    assert not seen.admits(_event(2, "run.started", run="R-0002", session="S-other"))
    other = _event(3, "run.started", run="R-0003",
                   payload={"cause": "rebuild", "for_runs": ["R-0002"]})  # fmt: skip
    assert not seen.admits(other)
    assert not seen.admits(_event(4, "run.ended", run="R-0003"))
    assert seen.admits(_event(5, "source.changed"))  # owner-wide facts still pass
