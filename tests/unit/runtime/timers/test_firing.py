"""Firing due timers (ADR-P25 D2, D3, D5): expiry voids, rechecks expire, reminders start runs.

A firing that starts work (a reminder) asks the hold predicate first and counts toward the
automatic-start limit; a firing that only voids or expires starts nothing and asks neither.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.budgets.limits import automatic_starts
from aksha.runtime.budgets.types import Limits
from aksha.runtime.canonical import canonical_json
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.interrupts.approvals import request_events
from aksha.runtime.interrupts.reducers import approvals, interrupts
from aksha.runtime.interrupts.types import ActionBody
from aksha.runtime.stop_policy.hold import held_auto_starts
from aksha.runtime.stop_policy.types import AgentScope, SessionScope
from aksha.runtime.timers.facts import scheduled
from aksha.runtime.timers.firing import fire_due
from aksha.runtime.timers.types import ApprovalRef, Reminder, TimerSet
from aksha.runtime.timers.view import timers
from tests.support.invalidation import OWNER, Bench, record, ref

AGENTS: Mapping[str, Mapping[str, Any]] = {"p": {"definition_hash": "sha256:" + "1" * 64}}


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    b = Bench(tmp_path)
    yield b
    b.close()


def _fire(b: Bench, **kw: Any) -> Any:
    return fire_due(b.store, OWNER, b.clock.now(), ids=b.ids, agents=AGENTS, **kw)


def _expiry(approval: str = "A-0001", after_ms: int = 60_000) -> TimerSet:
    return TimerSet(f"approval_expiry:{approval}", ApprovalRef(approval), "approval_expiry",
                    after_ms=after_ms)  # fmt: skip


def _request(b: Bench, approval: str = "A-0001", run: str = "R-0001") -> None:
    body = ActionBody(
        tool="send", owner=OWNER, canonical_args=canonical_json({}), targets=(),
        idempotency_key=f"send:{approval}", definition_hash="sha256:" + "0" * 64, facts={},
    )  # fmt: skip
    b.store.append(OWNER, "U4", (*request_events(approval, body, run=run, instance="x"),
                                 scheduled(_expiry(approval))))  # fmt: skip


def _remind(timer_id: str = "T-1", session: str = "S-0001", after_ms: int = 1_000) -> TimerSet:
    return TimerSet(timer_id, SessionScope(session), "reminder", after_ms=after_ms,
                    request=Reminder("p", "re-check the prices"))  # fmt: skip


# --- approval expiry (D5) --------------------------------------------------------------


def test_nothing_fires_before_it_is_due(bench: Bench) -> None:
    bench.start("R-0001")
    _request(bench)
    bench.clock.advance(59)
    assert _fire(bench).fired == ()
    assert approvals(bench.view())["A-0001"].state == "requested"


def test_a_due_expiry_voids_the_open_request_and_its_interrupt(bench: Bench) -> None:
    bench.start("R-0001")
    _request(bench)
    bench.clock.advance(60)
    report = _fire(bench)
    assert report.fired == ("approval_expiry:A-0001",)
    view = bench.view()
    a = approvals(view)["A-0001"]
    assert (a.state, a.void_reason) == ("voided", "expired")
    i = interrupts(view)["A-0001"]
    assert (i.state, i.void_reason) == ("voided", "expired")
    assert timers(view)["approval_expiry:A-0001"].state == "fired"
    assert _fire(bench).fired == ()


def test_expiry_is_not_held_by_the_approval_it_exists_to_void(bench: Bench) -> None:
    """D2: the open approval interrupt holds its run; the expiry still fires."""
    from aksha.runtime.stop_policy.hold import hold_reason
    from aksha.runtime.stop_policy.types import RunScope

    bench.start("R-0001")
    _request(bench)
    assert hold_reason(bench.view(), RunScope("R-0001")) is None
    from aksha.runtime.tasks.types import InstanceRef

    assert hold_reason(bench.view(), InstanceRef("R-0001", "x")) is not None
    bench.clock.advance(61)
    assert _fire(bench).fired == ("approval_expiry:A-0001",)
    assert hold_reason(bench.view(), InstanceRef("R-0001", "x")) is None
    assert held_auto_starts(bench.view()) == ()


def test_a_granted_unused_approval_expires_too(bench: Bench) -> None:
    bench.start("R-0001")
    bench.save("R-0001", "p.packet/C1", {"v": 1}, reads=[record("K")])
    bench.approve("A-0001", "p.packet/C1@v1")
    bench.store.append(OWNER, "U4", (scheduled(_expiry()),))
    bench.clock.advance(60)
    assert _fire(bench).fired == ("approval_expiry:A-0001",)
    assert approvals(bench.view())["A-0001"].state == "voided"


def test_an_expiry_of_a_decided_approval_is_cancelled_as_moot(bench: Bench) -> None:
    bench.start("R-0001")
    _request(bench)
    from aksha.runtime.interrupts.commands import reject

    assert reject(bench.store, OWNER, "A-0001", by="p").accepted
    bench.clock.advance(60)
    report = _fire(bench)
    assert report.fired == () and report.cancelled == ("approval_expiry:A-0001",)
    assert approvals(bench.view())["A-0001"].state == "rejected"
    (closed,) = [e for e in bench.events() if e.type == "timer.cancelled"]
    assert closed.payload == {"timer_id": "approval_expiry:A-0001", "reason": "moot"}


# --- recheck (D3) ----------------------------------------------------------------------


def test_a_due_recheck_expires_the_record_and_revokes_approvals_on_what_moved(
    bench: Bench,
) -> None:
    bench.start("R-0001")
    bench.save("R-0001", "p.packet/C1", {"v": 1}, reads=[record("K")])
    bench.approve("A-0001", "p.packet/C1@v1")
    timer = TimerSet("recheck:record:refs/K:1", AgentScope("p"), "recheck", after_ms=1_000,
                     target=record("K"))  # fmt: skip
    bench.store.append(OWNER, "U2", (scheduled(timer),))
    bench.clock.advance(1)
    assert _fire(bench).fired == ("recheck:record:refs/K:1",)
    types = [e.type for e in bench.events()][-3:]
    assert types == ["source.expired", "approval.revoked", "timer.fired"]
    assert bench.iv().state("p.packet/C1@v1").freshness == "invalidated"
    assert approvals(bench.view())["A-0001"].state == "revoked"


def test_an_artifact_recheck_expires_that_version(bench: Bench) -> None:
    bench.start("R-0001")
    bench.save("R-0001", "p.extract/X", {"v": 1}, reads=[record("K")])
    target = ref("p.extract/X@v1").as_input()
    timer = TimerSet("recheck:artifact:p.extract/X@v1:1", AgentScope("p"), "recheck",
                     after_ms=1, target=target)  # fmt: skip
    bench.store.append(OWNER, "U2", (scheduled(timer),))
    bench.clock.advance(1)
    _fire(bench)
    assert bench.iv().state("p.extract/X@v1").freshness == "invalidated"


# --- reminders (D2) --------------------------------------------------------------------


def test_a_due_reminder_starts_a_run_with_cause_timer_in_its_session(bench: Bench) -> None:
    bench.store.append(OWNER, "U1", (scheduled(_remind()),))
    bench.clock.advance(1)
    report = _fire(bench)
    assert report.fired == ("T-1",)
    (started,) = [e for e in bench.events() if e.type == "run.started"]
    assert started.session == "S-0001" and started.run is not None
    assert started.payload["cause"] == "timer"
    assert started.payload["timer"] == "T-1"
    assert started.payload["request"] == "re-check the prices"
    assert started.payload["definition_hash"] == AGENTS["p"]["definition_hash"]
    assert automatic_starts(bench.view(), started.run, bench.clock.now(), 300_000) == 1
    assert held_auto_starts(bench.view()) == ()


def test_a_reminder_for_an_agent_this_runtime_lacks_waits(bench: Bench) -> None:
    bench.store.append(OWNER, "U1", (scheduled(_remind()),))
    bench.clock.advance(1)
    report = fire_due(bench.store, OWNER, bench.clock.now(), ids=bench.ids, agents={})
    assert report.fired == () and report.deferred == {"T-1": "agent_unknown"}


def test_a_reminder_waits_while_its_session_has_an_open_run(bench: Bench) -> None:
    bench.store.append(OWNER, "U1", (NewEvent("run.started", {"agent": "p", "cause": "request"},
                                              run="R-0001", session="S-0001"),))  # fmt: skip
    bench.store.append(OWNER, "U1", (scheduled(_remind()),))
    bench.clock.advance(1)
    assert _fire(bench).deferred == {"T-1": "lane_required"}
    bench.end("R-0001")
    assert _fire(bench).fired == ("T-1",)


def test_a_reminder_never_wakes_a_scope_a_person_holds(bench: Bench) -> None:
    """A person's standing stop of the agent holds the reminder until they ask again."""
    from aksha.runtime.cancellation.command import stop
    from aksha.runtime.cancellation.ending import honour_stop

    bench.start("R-0001")
    assert stop(bench.store, OWNER, "R-0001", reason="enough", command_id="c1", by="p").accepted
    assert honour_stop(bench.store, OWNER, "R-0001", "resume")
    bench.store.append(OWNER, "U1", (scheduled(_remind()),))
    bench.clock.advance(1)
    report = _fire(bench)
    assert report.fired == () and report.deferred == {"T-1": "stopped_by_person"}
    assert held_auto_starts(bench.view()) == ()


def test_a_reminder_waits_while_its_session_has_an_open_route_question(bench: Bench) -> None:
    """ADR-P25 D2 with ADR-P17 S5: an open ``route`` question holds its session's intake."""
    from aksha.runtime.interrupts.raising import raised, route_request

    question = raised(route_request("S-0001", "which agent?", ("p",)), "I-0001")
    bench.store.append(OWNER, "U4", (question,))
    bench.store.append(OWNER, "U1", (scheduled(_remind()), scheduled(_remind("T-2", "S-0002"))))
    bench.clock.advance(1)
    report = _fire(bench)
    assert report.fired == ("T-2",) and report.deferred == {"T-1": "interrupt_open"}
    answer = {"interrupt": "I-0001", "answer": "p"}
    bench.store.append(OWNER, "U4", (NewEvent("interrupt.resolved", answer, session="S-0001"),))
    assert _fire(bench).fired == ("T-1",)
    assert held_auto_starts(bench.view()) == ()


def test_reminder_starts_are_capped_by_the_automatic_start_limit(bench: Bench) -> None:
    for n in range(1, 5):
        bench.store.append(OWNER, "U1", (scheduled(_remind(f"T-{n}", session=f"S-{n}")),))
    bench.clock.advance(1)
    report = _fire(bench, limits=Limits(auto_starts=(3, 300_000)))
    assert report.fired == ("T-1", "T-2", "T-3")
    assert report.deferred == {"T-4": "auto_start_limit"}
    bench.clock.advance(301)
    assert _fire(bench, limits=Limits(auto_starts=(3, 300_000))).fired == ("T-4",)


def test_each_firing_survives_a_crash_and_never_fires_twice(tmp_path: Path) -> None:
    b = Bench(tmp_path)
    b.start("R-0001")
    _request(b)
    b.close()
    again = Bench(tmp_path)
    again.clock.advance(60)
    assert _fire(again).fired == ("approval_expiry:A-0001",)
    assert _fire(again).fired == ()
    assert [e.type for e in again.events()].count("approval.voided") == 1
    again.close()
