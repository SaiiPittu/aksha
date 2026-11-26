"""ADR-P25 through the agent loop and the session: dated reads, lifetimes, expiry, ticks.

The loop bench's ``coder`` agent stands in for any agent: a body that counts days reads
``ToolContext.run_date`` (as the patent agent's tools do), a fetch returns a record read,
and the ``send`` tool asks for approval.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.interrupts.reducers import approvals
from aksha.runtime.interrupts.types import ApprovalRule
from aksha.runtime.invalidation.view import invalidation_view
from aksha.runtime.session import named as n
from aksha.runtime.timers.clock import CLOCK_RECORD, date_change, time_read
from aksha.runtime.timers.firing import fire_due
from aksha.runtime.timers.view import pending
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import Ok, ToolContext, ToolDecl, ToolOutcome
from tests.support.loop import (
    OWNER,
    RUN,
    SAVE_NOTE,
    Bench,
    Recorder,
    call,
    finish_reply,
    tools_reply,
)
from tests.support.session import SESSION, SessionBench

DAY1 = "2026-11-27"
DAY2 = "2026-11-28"


def _with_body(b: Bench, tool: str, body: Any) -> None:
    listed = {t.spec.name: t for t in b.agent.tools if isinstance(t, ToolDecl)}
    listed[tool] = ToolDecl(listed[tool].spec, body)
    b.setup = replace(b.setup, registry=ToolRegistry(b.agent, listed, ()))


def _days(seen: list[str | None]) -> Any:
    def body(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        seen.append(ctx.run_date)
        start = date(2026, 11, 1)
        today = date.fromisoformat(ctx.run_date or DAY1)
        return ToolOutcome(Ok({"days": (today - start).days}))

    return body


def _day_change(b: Bench, today: str) -> None:
    events = date_change(b.view(), date.fromisoformat(today))
    assert events
    b.store.append(OWNER, "U8", events)


@pytest.fixture
def dated(tmp_path: Path) -> Iterator[tuple[Bench, list[str | None]]]:
    model = Recorder([tools_reply(call("read_file", path="a.py")), finish_reply()])
    b = Bench(tmp_path / "aksha.sqlite", model)
    seen: list[str | None] = []
    _with_body(b, "read_file", _days(seen))
    b.start()
    yield b, seen
    b.close()


def test_a_body_that_reads_the_run_date_makes_its_output_depend_on_the_date(
    dated: tuple[Bench, list[str | None]],
) -> None:
    b, seen = dated
    assert b.drive().outcome == "complete"
    assert seen == [DAY1]
    (saved,) = b.events("artifact.saved")
    assert time_read(DAY1).as_json() in saved.payload["reads"]
    reads = [e.payload["input"] for e in b.events("read.recorded")]
    assert reads == [time_read(DAY1).as_json()]
    note = str(saved.payload["ref"])
    assert invalidation_view(b.view()).state(note).freshness == "current"
    b.clock.advance(86_400)
    _day_change(b, DAY2)
    state = invalidation_view(b.view()).state(note)
    assert state.freshness == "invalidated"
    assert [str(r.input) for r in state.changed_reads()] == [f"time:clock@{DAY1}"]


def test_a_replayed_dated_step_is_not_run_again_and_keeps_its_recorded_date(
    tmp_path: Path,
) -> None:
    model = Recorder([tools_reply(call("read_file", path="a.py")), finish_reply()])
    b = Bench(tmp_path / "aksha.sqlite", model)
    seen: list[str | None] = []
    _with_body(b, "read_file", _days(seen))
    b.start()
    b.drive()
    before = [e.payload for e in b.events("read.recorded")]
    b.clock.advance(86_400)
    b.restart()
    _with_body(b, "read_file", _days(seen))
    b.drive()
    assert seen == [DAY1]
    assert [e.payload for e in b.events("read.recorded")] == before
    b.close()


def test_after_a_recorded_day_boundary_a_body_counts_to_the_new_day(tmp_path: Path) -> None:
    model = Recorder([tools_reply(call("read_file", path="a.py")), finish_reply()])
    b = Bench(tmp_path / "aksha.sqlite", model)
    seen: list[str | None] = []
    _with_body(b, "read_file", _days(seen))
    b.start()
    b.clock.advance(86_400)
    _day_change(b, DAY2)
    assert b.drive().outcome == "complete"
    assert seen == [DAY2]
    (saved,) = b.events("artifact.saved")
    assert invalidation_view(b.view()).state(str(saved.payload["ref"])).freshness == "current"
    b.close()


def test_a_record_read_from_a_source_with_a_lifetime_schedules_its_recheck(
    tmp_path: Path,
) -> None:
    model = Recorder([tools_reply(call("read_file", path="a.py")), finish_reply()])
    b = Bench(tmp_path / "aksha.sqlite", model)
    page = InputRef("record", "pages/P-1", "v1")

    def fetch(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        return ToolOutcome(Ok({"text": "pricing"}, reads=(page,)))

    _with_body(b, "read_file", fetch)
    b.setup = replace(b.setup, lifetimes={"pages": 3_600})
    b.start()
    b.drive()
    (timer,) = pending(b.view())
    assert timer.spec.cause == "recheck" and timer.spec.target == page
    (scheduled,) = b.events("timer.scheduled")
    done = [e for e in b.events("step.completed") if e.payload.get("tool") == "read_file"]
    assert scheduled.step == done[0].step and scheduled.seq < done[0].seq
    b.clock.advance(3_600)
    assert fire_due(b.store, OWNER, b.clock.now(), ids=_ids(b)).fired == (timer.spec.timer_id,)
    (saved,) = b.events("artifact.saved")
    state = invalidation_view(b.view()).state(str(saved.payload["ref"]))
    assert state.freshness == "invalidated"
    b.close()


def _ids(b: Bench) -> Any:
    from aksha.runtime.execution.ids import LogIdSource

    return LogIdSource(b.store)


def test_an_approval_with_a_lifetime_expires_and_the_run_goes_on(tmp_path: Path) -> None:
    send = call("send", document="d.txt", recipient_id="r1")
    model = Recorder([tools_reply(send), tools_reply(SAVE_NOTE, call("finish", summary="x"))])
    b = Bench(tmp_path / "aksha.sqlite", model)
    listed = {t.spec.name: t for t in b.agent.tools if isinstance(t, ToolDecl)}
    spec = listed["send"].spec
    assert spec.approval is not None
    rule = ApprovalRule(spec.approval.facts, spec.approval.targets, expires_after_s=600)
    listed["send"] = ToolDecl(replace(spec, approval=rule), listed["send"].body)
    b.agent = replace(b.agent, tools=(*listed.values(), b.agent.tools[-1]))
    b.setup = replace(b.setup, agent=b.agent, registry=ToolRegistry(b.agent, listed, ()))
    b.start()
    assert b.drive().state == "held"
    (requested,) = b.events("approval.requested")
    (timer,) = pending(b.view())
    assert timer.spec.cause == "approval_expiry" and timer.due_at == "2026-11-27T09:10:00.000000Z"
    (scheduled,) = b.events("timer.scheduled")
    assert scheduled.step == requested.step and scheduled.run == RUN
    b.clock.advance(599)
    assert fire_due(b.store, OWNER, b.clock.now(), ids=_ids(b)).fired == ()
    b.clock.advance(1)
    assert fire_due(b.store, OWNER, b.clock.now(), ids=_ids(b)).fired == (timer.spec.timer_id,)
    a = approvals(b.view())[str(requested.payload["approval"])]
    assert (a.state, a.void_reason) == ("voided", "expired")
    assert b.drive().outcome == "complete"
    b.close()


# --- the session ticks ------------------------------------------------------------------


@pytest.fixture
def session(tmp_path: Path) -> Iterator[SessionBench]:
    b = SessionBench(tmp_path)
    yield b
    b.close()


def _send(text: str, cid: str) -> n.SendMessage:
    return n.SendMessage(owner=OWNER_S, session=SESSION, text=text, command_id=cid)


OWNER_S = "o1"


def test_a_session_records_the_new_day_before_a_run_open_across_it_moves(
    session: SessionBench,
) -> None:
    assert session.session.dispatch(_send("Write a note", "c-1")).accepted
    session.clock.advance(86_400)
    session.session.run_until_yield(OWNER_S)
    types = [e.type for e in session.events()]
    first = types.index("source.changed")
    assert first < types.index("stop.decided")
    (changed,) = session.payloads("source.changed")
    assert changed["cause"] == "clock" and changed["records"] == {CLOCK_RECORD: DAY2}


def test_a_same_day_session_writes_no_clock_fact(session: SessionBench) -> None:
    assert session.session.dispatch(_send("Write a note", "c-1")).accepted
    session.session.run_until_yield(OWNER_S)
    assert session.payloads("source.changed") == []


def test_a_session_step_fires_a_due_reminder_into_a_new_run(session: SessionBench) -> None:
    from aksha.runtime.stop_policy.types import SessionScope
    from aksha.runtime.timers.facts import scheduled
    from aksha.runtime.timers.types import Reminder, TimerSet

    timer = TimerSet("T-1", SessionScope(SESSION), "reminder", after_ms=60_000,
                     request=Reminder("scribe", "Write a note"))  # fmt: skip
    session.session.store.append(OWNER_S, "U1", (scheduled(timer),))
    assert session.session.store.owners_with_pending_timers() == (OWNER_S,)
    assert session.session.step(OWNER_S) is False
    session.clock.advance(60)
    session.session.run_until_yield(OWNER_S)
    (started,) = session.payloads("run.started")
    assert (started["cause"], started["timer"], started["agent"]) == ("timer", "T-1", "scribe")
    (ended,) = session.payloads("run.ended")
    assert ended["outcome"] == "complete"
    assert session.session.store.owners_with_pending_timers() == ()
