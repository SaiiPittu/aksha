"""Durable timers as facts (ADR-P25 D1): scheduled, then fired or cancelled, once."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.events.errors import PayloadInvalid
from aksha.runtime.stop_policy.types import AgentScope, RunScope, SessionScope
from aksha.runtime.storage.errors import AlreadyRecorded
from aksha.runtime.tasks.types import InstanceRef
from aksha.runtime.timers.facts import cancelled, fired, scheduled
from aksha.runtime.timers.types import ApprovalRef, Reminder, TimerScope, TimerSet
from aksha.runtime.timers.view import due, pending, timers
from tests.support.invalidation import OWNER, Bench


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    b = Bench(tmp_path)
    yield b
    b.close()


def _remind(timer_id: str = "T-1", **kw: object) -> TimerSet:
    values: dict[str, object] = {
        "timer_id": timer_id,
        "scope": SessionScope("S-0001"),
        "cause": "reminder",
        "due_at": "2026-11-27T10:00:00+00:00",
        "request": Reminder("scribe", "check the prices again"),
    }
    values.update(kw)
    return TimerSet(**values)  # type: ignore[arg-type]


def test_a_timer_names_exactly_one_time() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        _remind(due_at=None)
    with pytest.raises(ValueError, match="exactly one"):
        _remind(after_ms=5)
    with pytest.raises(ValueError, match="after_ms"):
        _remind(due_at=None, after_ms=-1)


def test_each_cause_carries_what_its_firing_needs() -> None:
    with pytest.raises(ValueError, match="approval"):
        TimerSet("T-2", RunScope("R-0001"), "approval_expiry", after_ms=1)
    with pytest.raises(ValueError, match="target"):
        TimerSet("T-3", AgentScope("p"), "recheck", after_ms=1)
    with pytest.raises(ValueError, match="request"):
        TimerSet("T-4", SessionScope("S-1"), "reminder", after_ms=1)
    with pytest.raises(ValueError, match="cause"):
        TimerSet("T-5", ApprovalRef("A-0001"), "wake", after_ms=1)  # type: ignore[arg-type]
    TimerSet("T-6", ApprovalRef("A-0001"), "approval_expiry", after_ms=1)
    TimerSet("T-7", AgentScope("p"), "recheck", after_ms=1, target=InputRef("record", "r/1", "1"))


def test_the_scheduled_fact_round_trips_every_scope(bench: Bench) -> None:
    scopes: tuple[TimerScope, ...] = (
        InstanceRef("R-0001", "collect", "k"),
        RunScope("R-0001"),
        AgentScope("p"),
        SessionScope("S-0001"),
    )
    for n, scope in enumerate(scopes, start=1):
        timer = TimerSet(
            f"T-{n}", scope, "recheck", due_at="2026-11-28T00:00:00+00:00",
            target=InputRef("record", "refs/K", "1"),
        )  # fmt: skip
        bench.store.append(OWNER, "U8", (scheduled(timer),))
    bench.store.append(OWNER, "U1", (scheduled(_remind("T-9")),))
    found = timers(bench.view())
    assert [found[f"T-{n}"].spec.scope for n in range(1, 5)] == list(scopes)
    assert found["T-9"].spec == _remind("T-9")


def test_a_timer_is_pending_until_it_fires_or_is_cancelled(bench: Bench) -> None:
    bench.store.append(OWNER, "U1", (scheduled(_remind("T-1")), scheduled(_remind("T-2"))))
    assert [t.spec.timer_id for t in pending(bench.view())] == ["T-1", "T-2"]
    bench.store.append(OWNER, "U1", (fired("T-1", "reminder"),))
    bench.store.append(OWNER, "U4", (cancelled("T-2", "moot"),))
    state = timers(bench.view())
    assert (state["T-1"].state, state["T-2"].state) == ("fired", "cancelled")
    assert pending(bench.view()) == ()


def test_a_timer_closes_once_whichever_fact_comes_first(bench: Bench) -> None:
    bench.store.append(OWNER, "U1", (scheduled(_remind()),))
    bench.store.append(OWNER, "U1", (fired("T-1", "reminder"),))
    with pytest.raises(AlreadyRecorded):
        bench.store.append(OWNER, "U4", (cancelled("T-1", "moot"),))
    with pytest.raises(AlreadyRecorded):
        bench.store.append(OWNER, "U1", (scheduled(_remind()),))


def test_due_reads_due_at_against_the_callers_clock_reading(bench: Bench) -> None:
    bench.store.append(OWNER, "U1", (scheduled(_remind()),))
    view = bench.view()
    assert due(view, "2026-11-27T09:59:59+00:00") == ()
    assert [t.timer_id for t in due(view, "2026-11-27T10:00:00+00:00")] == ["T-1"]


def test_after_ms_counts_from_the_scheduling_facts_recorded_time(bench: Bench) -> None:
    bench.clock.advance(30)
    bench.store.append(OWNER, "U1", (scheduled(_remind(due_at=None, after_ms=60_000)),))
    (event,) = [e for e in bench.events() if e.type == "timer.scheduled"]
    (timer,) = pending(bench.view())
    assert event.at == "2026-11-27T09:00:30.000000Z"
    assert timer.due_at == "2026-11-27T09:01:30.000000Z"
    assert due(bench.view(), "2026-11-27T09:01:29+00:00") == ()
    assert len(due(bench.view(), "2026-11-27T09:01:30+00:00")) == 1


def test_due_lists_the_earliest_first_and_skips_closed_timers(bench: Bench) -> None:
    late = _remind("T-1", due_at="2026-11-27T12:00:00+00:00")
    early = _remind("T-2", due_at="2026-11-27T11:00:00+00:00")
    gone = _remind("T-3", due_at="2026-11-27T10:00:00+00:00")
    bench.store.append(OWNER, "U1", (scheduled(late), scheduled(early), scheduled(gone)))
    bench.store.append(OWNER, "U4", (cancelled("T-3", "moot"),))
    assert [t.timer_id for t in due(bench.view(), "2026-11-28T00:00:00+00:00")] == ["T-2", "T-1"]


def test_a_malformed_timer_payload_is_refused_by_the_catalogue(bench: Bench) -> None:
    from aksha.runtime.events.envelope import NewEvent

    bad = NewEvent("timer.scheduled", {"timer_id": "T-1", "cause": "reminder"})
    with pytest.raises(PayloadInvalid):
        bench.store.append(OWNER, "U1", (bad,))


def test_timers_survive_a_restart(tmp_path: Path) -> None:
    b = Bench(tmp_path)
    b.store.append(OWNER, "U1", (scheduled(_remind()),))
    b.close()
    again = Bench(tmp_path)
    assert [t.spec.timer_id for t in pending(again.view())] == ["T-1"]
    assert again.store.owners_with_pending_timers() == (OWNER,)
    again.store.append(OWNER, "U1", (fired("T-1", "reminder"),))
    assert again.store.owners_with_pending_timers() == ()
    again.close()
