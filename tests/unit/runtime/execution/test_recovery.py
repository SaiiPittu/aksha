"""Recovery on startup and lazy resume (ADR-P02 D5, D6, D10) on a SQLite store."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.cancellation.facts import stop_requested
from aksha.runtime.cancellation.types import DEFAULT_REASON
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution import facts
from aksha.runtime.execution.errors import NotRecoveryWriter
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.ports import DurableStepExecutor
from aksha.runtime.execution.recovery import (
    classify_open_run,
    dangling_steps,
    recover_on_startup,
    recover_owner,
    resume_owner,
)
from aksha.runtime.execution.types import Deadline, StepId, StepOutput, StepPolicy, StepResult
from aksha.runtime.interrupts.reducers import pending_interrupts
from aksha.runtime.projections.runs import RunSummary
from aksha.runtime.projections.view import load_view
from aksha.runtime.storage.ports import EventStore
from tests.support.events import START
from tests.support.steps import HASH, Crash, Script, recovery_catalogue, started_run, types

STOP = stop_requested("R-0001", scope="run", reason=DEFAULT_REASON, by="o", command_id="c-1")
H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64
LOADED = {"a": H1}
ONCE = StepPolicy(semantics="at_most_once")


class Harness:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.clock = FakeClock(START)
        self.store: EventStore = self._open()

    def _open(self) -> EventStore:
        return open_store(self.path, self.clock, writer=True, catalogue=recovery_catalogue())

    def append(self, boundary: str, *events: NewEvent, owner: str = "o") -> None:
        self.store.append(owner, boundary, events)

    def executor(self, owner: str = "o") -> StepExecutor:
        return StepExecutor(self.store, owner, self.clock)

    def restart(self) -> None:
        self.store.close()
        self.store = self._open()


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path / "aksha.sqlite")
    yield harness
    harness.store.close()


def step(n: int, run: str = "R-0001") -> StepId:
    return StepId(run, "a", turn=1, call=n)


class Driver:
    """A run's code: three steps in order, then the run ends. Records what each returned."""

    def __init__(self, h: Harness, crash_at: int | None = None) -> None:
        self.h = h
        self.crash_at = crash_at
        self.script = Script()
        self.results: list[StepResult] = []
        self.driven: list[str] = []

    def __call__(self, run: RunSummary, steps: DurableStepExecutor) -> None:
        self.driven.append(run.run)
        for n in (1, 2, 3):
            work = self.script.crash(f"{n}") if n == self.crash_at else self.script.ok(f"{n}")
            self.results.append(steps.step(step(n, run.run), "tool", "fetch", HASH, work,
                                           StepPolicy()))  # fmt: skip
        ended = NewEvent("run.ended", {"outcome": "complete", "reason": "done"}, run=run.run)
        self.h.store.append("o", "U3", (ended,))


def test_dangling_steps_are_started_with_no_result(h: Harness) -> None:
    h.append("U1", started_run("R-0001", definition_hash=H1))
    ex = h.executor()
    ex.step(step(1), "tool", "fetch", HASH, Script().ok("1"), StepPolicy())
    with pytest.raises(Crash):
        ex.step(step(2), "tool", "send", HASH, Script().crash("2"), ONCE)
    (d,) = dangling_steps(load_view(h.store, "o"))
    assert (d.step_id, d.run, d.kind, d.name, d.semantics, d.dispatched, d.attempts) == (
        str(step(2)), "R-0001", "tool", "send", "at_most_once", True, 1,
    )  # fmt: skip
    assert dangling_steps(load_view(h.store, "o"), run="R-0002") == ()


@pytest.mark.parametrize(
    ("events", "loaded", "expected"),
    [
        ((), H1, "in_flight"),
        ((), H2, "definition_changed"),
        ((), None, "definition_changed"),
        ((STOP,), H1, "stop_requested"),
        ((STOP,), H2, "stop_requested"),  # a person's stop wins over a changed definition
        (
            (NewEvent("interrupt.raised", {"interrupt": "A-0001"}, run="R-0001"), STOP),
            H1,
            "stop_requested",
        ),
        ((NewEvent("interrupt.raised", {"interrupt": "A-0001"}, run="R-0001"),), H1, "suspended"),
        (
            (
                NewEvent("interrupt.raised", {"interrupt": "A-0001"}, run="R-0001"),
                NewEvent("interrupt.resolved", {"interrupt": "A-0001"}, run="R-0001"),
            ),
            H1,
            "in_flight",
        ),
        (
            (NewEvent("interrupt.raised", {"interrupt": "A-0001"}, run="R-0001"),),
            H2,
            "definition_changed",
        ),
        ((NewEvent("interrupt.raised", {"interrupt": "A-0001"}, run="R-0002"),), H1, "in_flight"),
    ],
)
def test_classify_open_run(
    h: Harness, events: tuple[NewEvent, ...], loaded: str | None, expected: str
) -> None:
    h.append("U1", started_run("R-0001", definition_hash=H1))
    for e in events:
        h.append("U2" if e.type == "interrupt.raised" else "U4", e)
    assert classify_open_run(load_view(h.store, "o"), "R-0001", loaded) == expected


def test_a_run_recorded_without_a_hash_is_a_changed_definition(h: Harness) -> None:
    h.append("U1", started_run("R-0001"))
    assert classify_open_run(load_view(h.store, "o"), "R-0001", H1) == "definition_changed"


def test_classify_refuses_a_run_that_is_not_open(h: Harness) -> None:
    with pytest.raises(ValueError, match="not an open run"):
        classify_open_run(load_view(h.store, "o"), "R-0009", H1)


def test_pending_interrupts_in_raise_order(h: Harness) -> None:
    h.append("U1", started_run("R-0001", definition_hash=H1))
    for i in ("A-0001", "A-0002", "A-0003"):
        h.append("U2", NewEvent("interrupt.raised", {"interrupt": i}, run="R-0001"))
    h.append("U4", NewEvent("interrupt.resolved", {"interrupt": "A-0002"}, run="R-0001"))
    assert pending_interrupts(load_view(h.store, "o"), "R-0001") == ("A-0001", "A-0003")


def test_recovery_closes_dispatched_at_most_once_steps_as_outcome_unknown(h: Harness) -> None:
    h.append("U1", started_run("R-0001", definition_hash=H1))
    script = Script()
    with pytest.raises(Crash):
        h.executor().step(step(1), "tool", "deploy", HASH, script.crash("deploy"), ONCE)
    h.restart()
    report = recover_on_startup(h.store, LOADED)
    assert report.closed_unknown == (("o", str(step(1))),)
    assert report.in_flight == {"o": ("R-0001",)}
    result = h.executor().step(step(1), "tool", "deploy", HASH, script.ok("again"), ONCE)
    assert (result.outcome, result.code, result.replayed) == (
        "interrupted", "outcome_unknown", True,
    )  # fmt: skip
    assert script.calls == ["deploy"]


def test_recovery_leaves_at_least_once_and_undispatched_steps_for_the_run(h: Harness) -> None:
    h.append("U1", started_run("R-0001", definition_hash=H1))
    with pytest.raises(Crash):
        h.executor().step(step(1), "tool", "fetch", HASH, Script().crash("x"), StepPolicy())
    h.append("U2a", facts.step_started(step(2), "tool", "send", HASH, ONCE))
    before = h.store.last_seq("o")
    report = recover_on_startup(h.store, LOADED)
    assert report.closed_unknown == ()
    assert h.store.last_seq("o") == before


def test_resume_under_changed_definition_ends_the_run_and_drives_nothing(h: Harness) -> None:
    h.append("U1", started_run("R-0001", definition_hash=H1))
    driver = Driver(h, crash_at=2)
    with pytest.raises(Crash):
        driver(RunSummary("R-0001", "a", "request"), h.executor())
    h.restart()
    report = recover_on_startup(h.store, {"a": H2})
    assert report.ended == (("o", "R-0001", "definition_changed"),)
    assert report.in_flight == {}
    *_, ended, record = h.store.read("o")
    assert (ended.type, dict(ended.payload)) == (
        "run.ended", {"outcome": "failed", "reason": "definition_changed"},
    )  # fmt: skip
    assert (record.type, dict(record.payload)) == (
        "stop.decided",
        {"walk": 1, "verdict": "stop", "cause": "resume", "reason": "definition_changed"},
    )  # the Run frame's resume record ends the run (ADR-P02 D7)
    assert len({ended.seq, record.seq}) == 2 and record.seq == ended.seq + 1
    again = Driver(h)
    assert resume_owner(h.store, "o", again, clock=h.clock, loaded={"a": H2}) == ()
    assert again.driven == []


def test_a_suspended_run_with_a_changed_definition_voids_its_interrupt(h: Harness) -> None:
    h.append("U1", started_run("R-0001", definition_hash=H1))
    h.append("U2", NewEvent("interrupt.raised", {"interrupt": "A-0001"}, run="R-0001"))
    report = recover_on_startup(h.store, {"a": H2})
    assert report.ended == (("o", "R-0001", "definition_changed"),)
    view = load_view(h.store, "o")
    assert pending_interrupts(view, "R-0001") == ()  # nobody is asked about an ended run
    tail = [(e.type, dict(e.payload)) for e in view.events][-3:]
    assert tail == [
        ("interrupt.voided", {"interrupt": "A-0001", "reason": "definition_changed"}),
        ("run.ended", {"outcome": "failed", "reason": "definition_changed"}),
        ("stop.decided",
         {"walk": 1, "verdict": "stop", "cause": "resume", "reason": "definition_changed"}),
    ]  # fmt: skip
    assert recover_on_startup(h.store, {"a": H2}).owners == ()


def test_stop_requested_run_is_ended_stopped(h: Harness) -> None:
    h.append("U1", started_run("R-0001", definition_hash=H1))
    h.append("U4", STOP)
    report = recover_on_startup(h.store, LOADED)
    assert report.ended == (("o", "R-0001", "stop_requested"),)
    (ended,) = [e for e in h.store.read("o") if e.type == "run.ended"]
    assert dict(ended.payload) == {"outcome": "stopped", "reason": "stop_requested"}


def test_suspended_run_untouched_by_restart(h: Harness) -> None:
    h.append("U1", started_run("R-0001", definition_hash=H1))
    h.executor().step(step(1), "tool", "fetch", HASH, Script().ok("1"), StepPolicy())
    h.append("U2", NewEvent("interrupt.raised", {"interrupt": "A-0001"}, run="R-0001"))
    for _ in range(2):
        h.restart()
        before = h.store.last_seq("o")
        report = recover_on_startup(h.store, LOADED)
        assert h.store.last_seq("o") == before
        assert report.owners[0].suspended == ("R-0001",)
        driver = Driver(h)
        assert resume_owner(h.store, "o", driver, clock=h.clock, loaded=LOADED) == ()
        assert driver.driven == []
        assert h.store.last_seq("o") == before


def test_resume_returns_recorded_results_and_continues_at_the_first_unfinished_step(
    h: Harness,
) -> None:
    h.append("U1", started_run("R-0001", definition_hash=H1))
    first = Driver(h, crash_at=2)
    with pytest.raises(Crash):
        first(RunSummary("R-0001", "a", "request"), h.executor())
    assert first.script.calls == ["1", "2"]
    h.restart()
    assert recover_on_startup(h.store, LOADED).in_flight == {"o": ("R-0001",)}
    second = Driver(h)
    assert resume_owner(h.store, "o", second, clock=h.clock, loaded=LOADED) == ("R-0001",)
    assert second.script.calls == ["2", "3"]  # step 1 was recorded: it does not run again
    assert [r.replayed for r in second.results] == [True, False, False]
    assert second.results[0].value == {"label": "1"}
    completed = [e.step for e in h.store.read("o") if e.type == "step.completed"]
    assert completed == [str(step(n)) for n in (1, 2, 3)]  # one result per step
    assert recover_on_startup(h.store, LOADED).owners == ()  # nothing open any more


def test_resume_is_lazy_and_per_owner(h: Harness) -> None:
    for owner in ("o", "p"):
        h.append("U1", started_run("R-0001", definition_hash=H1), owner=owner)
    report = recover_on_startup(h.store, LOADED)
    assert report.in_flight == {"o": ("R-0001",), "p": ("R-0001",)}
    assert types(h.store, "p") == ["run.started"]  # startup drove nothing
    driver = Driver(h)
    assert resume_owner(h.store, "o", driver, clock=h.clock, loaded=LOADED) == ("R-0001",)
    assert types(h.store, "p") == ["run.started"]  # owner p waits for its own command


def test_recovery_twice_writes_nothing_the_second_time(h: Harness) -> None:
    h.append("U1", started_run("R-0001", definition_hash=H1))
    h.append("U1", started_run("R-0002", definition_hash=H1))
    with pytest.raises(Crash):
        h.executor().step(step(1, "R-0002"), "tool", "deploy", HASH, Script().crash("d"), ONCE)
    recover_owner(h.store, "o", {"a": H2})
    after_first = h.store.last_seq("o")
    report = recover_owner(h.store, "o", {"a": H2})
    assert h.store.last_seq("o") == after_first
    assert report.ended == () and report.closed_unknown == ()


def test_a_finished_run_is_not_recovered(h: Harness) -> None:
    h.append("U1", started_run("R-0001", definition_hash=H1))
    with pytest.raises(Crash):
        h.executor().step(step(1), "tool", "deploy", HASH, Script().crash("d"), ONCE)
    h.append("U3", NewEvent("run.ended", {"outcome": "failed", "reason": "x"}, run="R-0001"))
    before = h.store.last_seq("o")
    assert recover_on_startup(h.store, LOADED).owners == ()
    assert recover_owner(h.store, "o", LOADED).closed_unknown == ()
    assert h.store.last_seq("o") == before


def _reader(h: Harness) -> EventStore:
    return open_store(h.path, h.clock, writer=False, catalogue=recovery_catalogue())


@pytest.mark.parametrize(
    "call",
    [
        lambda s: recover_on_startup(s, LOADED),
        lambda s: recover_owner(s, "o", LOADED),
    ],
)
def test_recovery_needs_the_writer(h: Harness, call: Callable[[EventStore], object]) -> None:
    h.append("U1", started_run("R-0001", definition_hash=H1))
    reader = _reader(h)
    try:
        with pytest.raises(NotRecoveryWriter) as e:
            call(reader)
        assert e.value.code == "not_writer"
    finally:
        reader.close()


def test_resumed_steps_see_a_real_deadline(h: Harness) -> None:
    h.append("U1", started_run("R-0001", definition_hash=H1))
    seen: list[int] = []

    def work(deadline: Deadline) -> StepOutput:
        seen.append(deadline.remaining_ms())
        return StepOutput({})

    h.executor().step(step(1), "tool", "f", HASH, work, StepPolicy(timeout_ms=5_000))
    assert 0 < seen[0] <= 5_000
