"""Crash a run at each named point, restart, and compare with a run that never crashed.

Runs against both storage adapters, so durable execution holds on PostgreSQL as on SQLite
(ADR-P02 "Where this stops", item 4: kill at every fault-injection point and assert the
log equals an uninterrupted run's). The run has three steps: a model call, a read
(``at_least_once``) whose first call fails transiently and is retried after a backoff, and
a send with no ledger (``at_most_once``).

"Equals" is read on the outcomes: the sequence of ``(step, outcome, value)`` results and
the run's ending must be those of the uninterrupted run. The bytes cannot be equal, since a
restart adds a resume record and a crash can eat an attempt; the one designed difference
is a send dispatched before the crash, which ends ``interrupted{outcome_unknown}`` and is
never sent again. A crash here is an in-process ``BaseException``;
``test_sigkill_recovery.py`` kills a real process at every commit point.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.runtime.budgets.types import Backoff
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.errors import TransientStepError
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.ports import DurableStepExecutor
from aksha.runtime.execution.recovery import recover_on_startup, resume_owner
from aksha.runtime.execution.types import Deadline, StepId, StepOutput, StepPolicy
from aksha.runtime.projections.runs import RunSummary
from aksha.runtime.storage.ports import EventStore
from tests.support.backends import Backend, PostgresBackend, SQLiteBackend
from tests.support.events import START, probe_catalogue
from tests.support.steps import HASH, Crash, started_run

H1 = "sha256:" + "1" * 64
LOADED = {"a": H1}
READ = StepPolicy(backoff=Backoff(base_ms=500, factor=2, cap_ms=4_000))
STEPS: tuple[tuple[str, str, StepPolicy], ...] = (
    ("model", "draft", StepPolicy()),
    ("tool", "fetch", READ),
    ("tool", "send", StepPolicy(semantics="at_most_once")),
)
CRASH_POINTS = (
    "between_steps",  # dies after step 1 committed, before step 2 starts
    "inside_read",  # dies during the read's first attempt
    "during_backoff",  # dies after the read's step.failed committed, while it backs off
    "inside_retry",  # dies during the read's second attempt
    "after_dispatch",  # dies after the send's dispatch marker
)


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Backend]:
    if request.param == "sqlite":
        yield SQLiteBackend(tmp_path)
        return
    yield PostgresBackend(request.getfixturevalue("postgres_dsn"))


class World:
    """The outside world across restarts: what ran, and a clock that can die in a sleep."""

    def __init__(self, crash: str | None = None) -> None:
        self.crash = crash
        self.died = False
        self.calls: list[str] = []
        self.clock = DyingClock(START, die=crash == "during_backoff")

    def work(self, name: str) -> Any:
        def run(deadline: Deadline) -> StepOutput:
            self.calls.append(name)
            if name == "fetch":
                n = self.calls.count("fetch")
                if (self.crash, n) in (("inside_read", 1), ("inside_retry", 2)):
                    raise Crash(f"inside fetch call {n}")
                if n == 1:
                    raise TransientStepError(message="the service is busy")
            if name == "send" and self.crash == "after_dispatch" and self.calls.count("send") == 1:
                raise Crash("after the send's dispatch")
            return StepOutput({"label": name})

        return run


class DyingClock(FakeClock):
    """A fake clock whose first sleep kills the process, if asked."""

    def __init__(self, start: Any, *, die: bool) -> None:
        super().__init__(start)
        self.die = die

    def sleep(self, seconds: float) -> None:
        if self.die:
            self.die = False
            raise Crash("during the backoff")
        super().sleep(seconds)


class Driver:
    def __init__(self, store: EventStore, world: World) -> None:
        self.store = store
        self.world = world

    def __call__(self, run: RunSummary, steps: DurableStepExecutor) -> None:
        for n, (kind, name, policy) in enumerate(STEPS, start=1):
            if self.world.crash == "between_steps" and n == 2 and not self.world.died:
                self.world.died = True
                raise Crash("between steps")
            sid = StepId(run.run, "a", turn=1, call=n)
            steps.step(sid, kind, name, HASH, self.world.work(name), policy)  # type: ignore[arg-type]
        ended = NewEvent("run.ended", {"outcome": "complete", "reason": "done"}, run=run.run)
        self.store.append("o", "U3", (ended,))


def _open(backend: Backend, clock: FakeClock) -> EventStore:
    return backend.open(clock, writer=True, catalogue=probe_catalogue())


def _outcomes(store: EventStore) -> list[tuple[str, Any, Any, Any]]:
    """The run's results in order: each step's (step, outcome, value), then the ending."""
    out: list[tuple[str, Any, Any, Any]] = []
    for e in store.read("o"):
        if e.type == "step.completed":
            value = e.payload.get("value")
            out.append((str(e.step), e.payload["outcome"], None if value is None else dict(value),
                        e.payload.get("code")))  # fmt: skip
        elif e.type == "run.ended":
            out.append(("run.ended", e.payload["outcome"], e.payload["reason"], None))
    return out


def _uninterrupted(backend: Backend) -> tuple[list[tuple[str, Any, Any, Any]], list[str]]:
    world = World()
    store = _open(backend, world.clock)
    try:
        store.append("o", "U1", (started_run("R-0001", definition_hash=H1),))
        Driver(store, world)(RunSummary("R-0001", "a", "request"),
                             StepExecutor(store, "o", world.clock))  # fmt: skip
        assert recover_on_startup(store, LOADED).owners == ()
        return _outcomes(store), world.calls
    finally:
        store.close()


def test_uninterrupted_run(backend: Backend) -> None:
    outcomes, calls = _uninterrupted(backend)
    assert calls == ["draft", "fetch", "fetch", "send"]  # the read is retried once
    assert [o[1] for o in outcomes] == ["ok", "ok", "ok", "complete"]


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_crash_and_restart_runs_no_step_twice(backend: Backend, tmp_path: Path, point: str) -> None:
    world = World(point)
    store = _open(backend, world.clock)
    store.append("o", "U1", (started_run("R-0001", definition_hash=H1),))
    with pytest.raises(Crash):
        Driver(store, world)(RunSummary("R-0001", "a", "request"),
                             StepExecutor(store, "o", world.clock))  # fmt: skip
    store.close()  # the process dies

    store = _open(backend, world.clock)
    try:
        report = recover_on_startup(store, LOADED)
        resumed = resume_owner(store, "o", Driver(store, world), clock=world.clock, loaded=LOADED)
        assert resumed == ("R-0001",)
        completed = [s for s, *_ in _outcomes(store) if s != "run.ended"]
        assert len(completed) == len(set(completed)) == 3  # one result per step
        types = [e.type for e in store.read("o")]
        assert types.count("step.started") == 3
        assert world.calls.count("draft") == 1  # a recorded model call is never paid twice
        assert world.calls.count("send") == 1  # the send never runs twice
        assert _outcomes(store) == _expected(tmp_path / "reference", point)
        if point == "after_dispatch":
            assert report.closed_unknown == (("o", "R-0001/a/t01/c03"),)
        resumes = [dict(e.payload) for e in store.read("o") if e.type == "stop.decided"]
        assert resumes == [
            {"walk": 1, "verdict": "continue", "cause": "resume", "resumes_turn": True}
        ]
        assert recover_on_startup(store, LOADED).owners == ()
    finally:
        store.close()


def _expected(reference: Path, point: str) -> list[tuple[str, Any, Any, Any]]:
    """The uninterrupted run's outcomes, with the one designed difference for ``point``.

    The reference run uses its own SQLite store: the outcomes do not depend on the backend.
    """
    reference.mkdir()
    outcomes, _ = _uninterrupted(SQLiteBackend(reference))
    if point == "after_dispatch":
        send = "R-0001/a/t01/c03"
        return [(send, "interrupted", None, "outcome_unknown") if s == send else (s, *rest)
                for s, *rest in outcomes]  # fmt: skip
    return outcomes
