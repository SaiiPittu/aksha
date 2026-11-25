"""Cancellation end to end (ADR-P18): a person's stop outlives the process that received it.

Runs against both storage adapters. A crash is a ``Crash`` (a ``BaseException``) or a
store closed and opened again between two commits, as a killed process would leave it.
The tests named in the ADR's Tests section keep their names.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.runtime.cancellation.command import stop
from aksha.runtime.cancellation.ending import honour_stop
from aksha.runtime.cancellation.hold import held_auto_starts, stop_hold
from aksha.runtime.cancellation.requests import stop_requests
from aksha.runtime.cancellation.types import DEFAULT_REASON, STOPPED_BY_PERSON
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution import recovery
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.ports import DurableStepExecutor
from aksha.runtime.execution.recovery import recover_on_startup, resume_owner
from aksha.runtime.execution.types import StepId, StepOutput, StepPolicy, StepResult
from aksha.runtime.projections.runs import RunSummary
from aksha.runtime.projections.view import load_view
from aksha.runtime.storage.ports import EventStore
from tests.support.backends import Backend, PostgresBackend, SQLiteBackend
from tests.support.events import START
from tests.support.steps import HASH, Crash, Script, recovery_catalogue, started_run

H1 = "sha256:" + "1" * 64
LOADED = {"a": H1}


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Backend]:
    if request.param == "sqlite":
        yield SQLiteBackend(tmp_path)
        return
    yield PostgresBackend(request.getfixturevalue("postgres_dsn"))


class World:
    """One store on one backend that can be killed and opened again."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.clock = FakeClock(START)
        self.store: EventStore = self._open()

    def _open(self) -> EventStore:
        return self.backend.open(self.clock, writer=True, catalogue=recovery_catalogue())

    def restart(self) -> None:
        self.store.close()
        self.store = self._open()

    def close(self) -> None:
        self.store.close()

    def start(self, run: str, agent: str = "a") -> None:
        self.store.append("o", "U1", (started_run(run, agent=agent, definition_hash=H1),))

    def facts(self, after: int = 0) -> list[tuple[str, str | None, dict[str, Any]]]:
        return [(e.type, e.run, dict(e.payload)) for e in self.store.read("o", after_seq=after)]


@pytest.fixture
def world(backend: Backend) -> Iterator[World]:
    w = World(backend)
    yield w
    w.close()


class Driver:
    """A run's code: three tool calls in one turn, then the run ends complete.

    A step the executor skips ends the turn at once, as the agent loop will (ADR-P18 D5).
    ``stop_after`` commits a person's stop from inside that call; ``crash_at`` kills the
    process inside that call.
    """

    def __init__(
        self, w: World, *, stop_after: int | None = None, crash_at: int | None = None
    ) -> None:
        self.w = w
        self.stop_after = stop_after
        self.crash_at = crash_at
        self.script = Script()
        self.results: list[StepResult] = []

    def __call__(self, run: RunSummary, steps: DurableStepExecutor) -> None:
        for n in (1, 2, 3):
            if self.crash_at == n:
                work = self.script.crash(str(n))
            elif self.stop_after == n:
                work = self._stopping(run.run, str(n))
            else:
                work = self.script.ok(str(n))
            sid = StepId(run.run, "a", turn=1, call=n)
            result = steps.step(sid, "tool", "fetch", HASH, work, StepPolicy())
            self.results.append(result)
            if result.outcome == "skipped":
                return  # the turn ends; the next stop candidate honours the stop
        ended = NewEvent("run.ended", {"outcome": "complete", "reason": "done"}, run=run.run)
        self.w.store.append("o", "U3", (ended,))

    def _stopping(self, run: str, label: str) -> Any:
        def work(deadline: Any) -> StepOutput:
            self.script.calls.append(label)
            stop(self.w.store, "o", run, command_id="cli-1")
            return StepOutput({"label": label})

        return work


def _run_ended(w: World, run: str) -> list[dict[str, Any]]:
    return [p for t, r, p in w.facts() if t == "run.ended" and r == run]


def _steps_started(w: World, after: int) -> list[str]:
    return [t for t, _, _ in w.facts(after) if t == "step.started"]


def test_stop_survives_crash(world: World) -> None:
    w = world
    w.start("R-0001")
    first = Driver(w, crash_at=2)
    with pytest.raises(Crash):
        first(RunSummary("R-0001", "a", "request"), StepExecutor(w.store, "o", w.clock))
    stop(w.store, "o", "R-0001", command_id="cli-1")  # committed before the kill
    w.restart()
    before = w.store.last_seq("o")
    report = recover_on_startup(w.store, LOADED)
    assert report.ended == (("o", "R-0001", "stop_requested"),)
    assert _run_ended(w, "R-0001") == [{"outcome": "stopped", "reason": "stop_requested"}]
    again = Driver(w)
    assert resume_owner(w.store, "o", again, clock=w.clock, loaded=LOADED) == ()
    assert again.script.calls == [] and _steps_started(w, before) == []
    w.restart()
    settled = w.store.last_seq("o")
    recover_on_startup(w.store, LOADED)
    assert w.store.last_seq("o") == settled  # a second kill and restart writes nothing


def test_stop_while_approval_pending_then_crash(world: World) -> None:
    w = world
    w.start("R-0002")
    steps = StepExecutor(w.store, "o", w.clock)
    raised = (
        NewEvent("approval.requested", {"approval": "A-0001"}, run="R-0002"),
        NewEvent("interrupt.raised", {"interrupt": "P-0009", "kind": "approval"}, run="R-0002"),
    )
    sid = StepId("R-0002", "a", turn=2, call=1)
    steps.step(sid, "tool", "handoff_request", HASH,
               lambda d: StepOutput({"asked": True}, events=raised), StepPolicy())  # fmt: skip
    hold = NewEvent("stop.decided", {"walk": 1, "verdict": "hold", "cause": "turn_end",
                                     "candidate": "turn_end"}, run="R-0002")  # fmt: skip
    w.store.append("o", "U3", (hold,))
    stop(w.store, "o", "R-0002", command_id="cli-7f3a")
    w.restart()  # the process dies before any later candidate
    before = w.store.last_seq("o")
    recover_on_startup(w.store, LOADED)
    assert w.facts(before) == [
        ("interrupt.voided", "R-0002", {"interrupt": "P-0009", "reason": "run_stopped"}),
        ("run.ended", "R-0002", {"outcome": "stopped", "reason": "stop_requested"}),
        ("stop.decided", "R-0002", {"walk": 2, "verdict": "stop", "cause": "resume",
                                    "candidate": "resume", "reason": "stop_requested"}),
    ]  # fmt: skip
    approvals = [t for t, _, p in w.facts() if t.startswith("approval.")]
    assert approvals == ["approval.requested"]  # A-0001 is still requested and decidable
    assert _steps_started(w, before) == []
    view = load_view(w.store, "o")
    hold_now = stop_hold(view, agent="a")
    assert hold_now is not None and hold_now.reason == STOPPED_BY_PERSON
    for _ in range(2):
        w.restart()
        settled = w.store.last_seq("o")
        recover_on_startup(w.store, LOADED)
        assert resume_owner(w.store, "o", Driver(w), clock=w.clock, loaded=LOADED) == ()
        assert w.store.last_seq("o") == settled


def test_stop_skips_unstarted_calls(world: World) -> None:
    w = world
    w.start("R-0001")
    driver = Driver(w, stop_after=1)
    driver(RunSummary("R-0001", "a", "request"), StepExecutor(w.store, "o", w.clock))
    assert [r.outcome for r in driver.results] == ["ok", "skipped"]
    assert driver.script.calls == ["1"]
    assert honour_stop(w.store, "o", "R-0001", "turn_end")  # the next stop candidate
    assert _run_ended(w, "R-0001") == [{"outcome": "stopped", "reason": "stop_requested"}]


def test_stop_skips_every_unstarted_call_of_the_turn(world: World) -> None:
    w = world
    w.start("R-0001")
    stop(w.store, "o", "R-0001", command_id="cli-1")
    steps = StepExecutor(w.store, "o", w.clock)
    script = Script()
    outcomes = [
        steps.step(StepId("R-0001", "a", turn=1, call=n), "tool", "fetch", HASH,
                   script.ok(str(n)), StepPolicy()).outcome
        for n in (2, 3)
    ]  # fmt: skip
    assert outcomes == ["skipped", "skipped"] and script.calls == []
    skipped = [p for t, _, p in w.facts() if t == "step.completed"]
    assert [(p["outcome"], p["code"]) for p in skipped] == [("skipped", "run_stopped")] * 2


def test_resume_honours_a_stop_the_driver_reached(world: World) -> None:
    w = world
    w.start("R-0001")
    with pytest.raises(Crash):
        Driver(w, crash_at=1)(RunSummary("R-0001", "a", "request"),
                              StepExecutor(w.store, "o", w.clock))  # fmt: skip
    w.restart()
    second = Driver(w, stop_after=2)
    assert resume_owner(w.store, "o", second, clock=w.clock, loaded=LOADED) == ("R-0001",)
    assert [r.outcome for r in second.results] == ["ok", "ok", "skipped"]
    assert _run_ended(w, "R-0001") == [{"outcome": "stopped", "reason": "stop_requested"}]


def test_stopped_is_not_partial(world: World) -> None:
    w = world
    w.start("R-0001")
    stop(w.store, "o", "R-0001", reason="editing the wrong file", by="web:person-1",
         command_id="web-1")  # fmt: skip
    w.restart()
    recover_on_startup(w.store, LOADED)
    (ended,) = _run_ended(w, "R-0001")
    assert ended["outcome"] == "stopped" and ended["outcome"] != "partial"
    (request,) = stop_requests(load_view(w.store, "o"), "R-0001")
    assert (request.reason, request.by, request.command_id) == (
        "editing the wrong file", "web:person-1", "web-1",
    )  # fmt: skip
    assert request.honoured_by is not None


def test_stop_is_idempotent_and_the_run_ends_once(world: World) -> None:
    w = world
    w.start("R-0001")
    stop(w.store, "o", "R-0001", command_id="c-1")
    stop(w.store, "o", "R-0001", command_id="c-1")
    stop(w.store, "o", "R-0001", command_id="c-2")
    view = load_view(w.store, "o")
    assert [r.command_id for r in stop_requests(view, "R-0001")] == ["c-1", "c-2"]
    w.restart()
    recover_on_startup(w.store, LOADED)
    assert len(_run_ended(w, "R-0001")) == 1
    requests = stop_requests(load_view(w.store, "o"), "R-0001")
    assert {r.honoured_by for r in requests} != {None}
    assert len({r.honoured_by for r in requests}) == 1
    assert stop(w.store, "o", "R-0001", command_id="c-3").refused == "run_not_open"


def _two_runs_one_stopped(w: World) -> None:
    """R-0001 and R-0002 of one agent are mid-turn; the person stops R-0001; the process dies."""
    for run in ("R-0001", "R-0002"):
        w.start(run)
        with pytest.raises(Crash):
            Driver(w, crash_at=2)(RunSummary(run, "a", "request"),
                                  StepExecutor(w.store, "o", w.clock))  # fmt: skip
    stop(w.store, "o", "R-0001", command_id="cli-1")
    w.restart()


def test_no_auto_start_after_person_stop(world: World) -> None:
    """TC-4: human state beats every automatic start, proved over the whole log."""
    w = world
    _two_runs_one_stopped(w)
    report = recover_on_startup(w.store, LOADED)
    assert report.ended == (("o", "R-0001", "stop_requested"),)
    assert report.owners[0].held == ("R-0002",)
    held = Driver(w)
    for _ in range(2):
        assert resume_owner(w.store, "o", held, clock=w.clock, loaded=LOADED) == ()
    assert held.script.calls == []
    assert held_auto_starts(load_view(w.store, "o")) == ()
    w.start("R-0003")  # the person's next command naming the agent: a new run
    resumed = Driver(w)
    driven = resume_owner(w.store, "o", resumed, clock=w.clock, loaded=LOADED)
    assert driven == ("R-0002", "R-0003")  # the new run, never driven yet, is in flight too
    assert resumed.script.calls == ["2", "3", "1", "2", "3"]
    assert held_auto_starts(load_view(w.store, "o")) == ()


def test_the_audit_fails_when_recovery_forgets_the_hold(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutation: recovery's start path without the hold predicate is caught by the audit."""
    w = world
    _two_runs_one_stopped(w)
    monkeypatch.setattr(recovery, "stop_hold", lambda view, **scope: None)
    assert resume_owner(w.store, "o", Driver(w), clock=w.clock, loaded=LOADED) == ("R-0002",)
    (found,) = held_auto_starts(load_view(w.store, "o"))
    assert (found.run, found.reason) == ("R-0002", STOPPED_BY_PERSON)


def test_the_default_reason_is_stopped_by_user(world: World) -> None:
    w = world
    w.start("R-0001")
    stop(w.store, "o", "R-0001", command_id="c-1")
    (request,) = stop_requests(load_view(w.store, "o"), "R-0001")
    assert request.reason == DEFAULT_REASON == "Stopped by user"
