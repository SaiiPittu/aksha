"""Budgets, limits and the restart limit on both storage adapters (ADR-P14).

Every count is read back from the event store, so each test also closes and reopens the
store and checks the number did not reset. The stop policy (ADR-P16) is not built, so the
tests write its records the way it will: a ``stop.decided`` CONTINUE per authorised turn,
and ``budget.reached`` with a HOLD when the meter shows a dimension spent.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.runtime.budgets.facts import budget_granted, budget_reached
from aksha.runtime.budgets.limits import attempt_number, resumes_since_progress
from aksha.runtime.budgets.meter import meter
from aksha.runtime.budgets.types import Budget
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.errors import TransientStepError
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.ports import DurableStepExecutor
from aksha.runtime.execution.recovery import recover_on_startup, resume_owner
from aksha.runtime.execution.types import Deadline, StepId, StepOutput, StepPolicy
from aksha.runtime.policy.events import decision_event
from aksha.runtime.policy.gate import check
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.runs import RUNS, RunSummary
from aksha.runtime.projections.view import load_view
from aksha.runtime.storage.ports import EventStore
from tests.support import gate
from tests.support.backends import Backend, PostgresBackend, SQLiteBackend
from tests.support.events import START, artifact_row
from tests.support.steps import HASH, Crash, Script, recovery_catalogue

RUN = "R-0001"
H1 = "sha256:" + "1" * 64
LOADED = {"a": H1}
BUDGET = Budget(model_turns=3, tool_calls=4, active_ms=120_000)


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Backend]:
    if request.param == "sqlite":
        yield SQLiteBackend(tmp_path)
        return
    yield PostgresBackend(request.getfixturevalue("postgres_dsn"))


class World:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.clock = FakeClock(START)
        self.store = self._open()

    def _open(self) -> EventStore:
        return self.backend.open(self.clock, writer=True, catalogue=recovery_catalogue())

    def restart(self) -> None:
        self.store.close()
        self.store = self._open()

    def start(self, budget: Budget = BUDGET) -> None:
        payload = {"agent": "a", "cause": "request", "definition_hash": H1,
                   "budget": budget.as_payload()}  # fmt: skip
        self.store.append("o", "U1", (NewEvent("run.started", payload, run=RUN),))

    def steps(self) -> StepExecutor:
        return StepExecutor(self.store, "o", self.clock)

    def walk(self) -> int:
        return 1 + sum(1 for e in self.store.read("o", run=RUN) if e.type == "stop.decided")

    def decide(self, verdict: str, *events: NewEvent, **fields: object) -> None:
        record = NewEvent("stop.decided", {"walk": self.walk(), "verdict": verdict, **fields},
                          run=RUN, instance="gather")  # fmt: skip
        self.store.append("o", "U3", (*events, record))


@pytest.fixture
def w(backend: Backend) -> Iterator[World]:
    world = World(backend)
    yield world
    world.store.close()


def _tool_call(w: World, n: int) -> str:
    """One turn's tool call through the gate and the executor: ``allowed`` or the refusal."""
    m = meter(load_view(w.store, "o"), RUN)
    spy = gate.Spy()
    agent = gate.agent(spy)
    decision = check(gate.gate_input(agent, "gather", "fetch", meter=m.reading()),
                     gate.registry(agent))  # fmt: skip
    sid = StepId(RUN, "gather", turn=1, call=n)
    call = gate.gate_input(agent, "gather", "fetch").call
    event = decision_event(decision, call, run=RUN, instance="gather", step=str(sid))
    if event.type == "step.completed":
        w.store.append("o", "U2", (event,))
        return str(event.payload["code"])
    save = StepOutput({"n": n}, artifacts=(artifact_row(f"note/A@v{n}"),),
                      events=(_saved(n),)) if n == 1 else StepOutput({"n": n})  # fmt: skip
    w.steps().step(sid, "tool", "fetch", HASH, lambda d: save, StepPolicy(),
                   extra={"route": "dispatch", "position": n - 1})  # fmt: skip
    return "allowed"


def _saved(n: int) -> NewEvent:
    row = artifact_row(f"note/A@v{n}")
    payload = {"ref": row.ref, "kind": row.kind, "key": row.key, "version": row.version,
               "content_hash": row.content_hash}  # fmt: skip
    return NewEvent("artifact.saved", payload, run=RUN, instance="gather")


def test_budget_tool_calls(w: World) -> None:
    w.start()
    answers = [_tool_call(w, n) for n in range(1, 7)]
    assert answers == ["allowed"] * 4 + ["budget_exhausted"] * 2
    w.restart()
    m = meter(load_view(w.store, "o"), RUN)
    assert m.used["tool_calls"] == 4 == m.allowed["tool_calls"]  # refusals at the limit: free
    assert m.exhausted() == ("tool_calls",)
    w.decide("hold", budget_reached(RUN, "I-0001", m), reason="budget")
    assert len(w.store.read_artifacts("o")) == 1  # the partial work is intact
    reached = [e.payload for e in w.store.read("o") if e.type == "budget.reached"]
    assert reached == [{"interrupt_id": "I-0001", "limit": "tool_calls", "used": 4,
                        "allowed": 4, "partial": True}]  # fmt: skip


def test_budget_model_turns(w: World) -> None:
    w.start()
    turns = 0
    while not meter(load_view(w.store, "o"), RUN).exhausted():
        turns += 1
        w.decide("continue", turn=turns)
    w.restart()
    m = meter(load_view(w.store, "o"), RUN)
    assert turns == m.used["model_turns"] == 3
    assert m.exhausted() == ("model_turns",)


def test_budget_active_ms(w: World) -> None:
    w.start(Budget(active_ms=120_000))

    def slow(deadline: Deadline) -> StepOutput:
        w.clock.advance(50)  # 50 s of injected time per call
        return StepOutput({"ok": True})

    calls = 0
    while not meter(load_view(w.store, "o"), RUN).exhausted():
        calls += 1
        w.steps().step(StepId(RUN, "gather", turn=1, call=calls), "tool", "fetch", HASH, slow,
                       StepPolicy())  # fmt: skip
    w.restart()
    m = meter(load_view(w.store, "o"), RUN)
    assert calls == 3 and m.used["active_ms"] == 150_000
    assert m.exhausted() == ("active_ms",)


def test_backoff_counts_as_active_time(w: World) -> None:
    w.start()
    script = Script()
    work = script.raises("f", TransientStepError(), TransientStepError())
    w.steps().step(StepId(RUN, "gather", turn=1, call=1), "tool", "fetch", HASH, work,
                   StepPolicy())  # fmt: skip
    m = meter(load_view(w.store, "o"), RUN)
    assert m.used["active_ms"] == 500 + 1_000
    assert m.used["tool_calls"] == 1  # three attempts, one call


def test_grant_extends_the_allowance(w: World) -> None:
    w.start(Budget(tool_calls=1))
    assert _tool_call(w, 1) == "allowed"
    assert _tool_call(w, 2) == "budget_exhausted"
    grant = budget_granted(RUN, "I-0001", "person", {"tool_calls": 2})
    w.store.append("o", "U4", (grant,))
    w.restart()
    assert [_tool_call(w, n) for n in (3, 4, 5)] == ["allowed", "allowed", "budget_exhausted"]


def test_attempt_count_survives_restart(w: World) -> None:
    w.start()
    sid = StepId(RUN, "gather", turn=1, call=1)
    script = Script()
    first = script.raises("f", TransientStepError(), Crash("during attempt 2"))
    with pytest.raises(Crash):
        w.steps().step(sid, "tool", "fetch", HASH, first, StepPolicy())
    w.restart()
    assert attempt_number(load_view(w.store, "o"), str(sid)) == 2
    again = script.raises("g", *(TransientStepError() for _ in range(5)))
    result = w.steps().step(sid, "tool", "fetch", HASH, again, StepPolicy())
    assert (result.outcome, result.attempts) == ("error", 3)  # max_retries=2: three in all
    # The crashed attempt 2 counts (ADR-P14 B1): the resume runs attempt 3 only.
    assert script.calls == ["f", "f", "g"]
    failed = [(e.payload["attempt"], e.payload["code"])
              for e in w.store.read("o", run=RUN) if e.type == "step.failed"]  # fmt: skip
    assert failed == [(1, "transient"), (2, "interrupted"), (3, "transient")]


def test_a_crash_loop_runs_no_more_than_max_retries_plus_one_attempts(w: World) -> None:
    # ADR-P14 B1: an attempt a crash interrupts is counted from the step's own records when
    # it resumes, so a body that kills the process every time runs three times, not forever,
    # and each interrupted attempt is charged active time, capped at the step's timeout.
    w.start()
    sid = StepId(RUN, "gather", turn=1, call=1)
    policy = StepPolicy(timeout_ms=30_000)  # max_retries=2
    runs = 0

    def dies(deadline: Deadline) -> StepOutput:
        nonlocal runs
        runs += 1
        w.clock.advance(5 if runs == 1 else 120)  # 5 s, then past the timeout
        raise Crash(f"the body kills the process, attempt {runs}")

    for _ in range(3):
        with pytest.raises(Crash):
            w.steps().step(sid, "tool", "fetch", HASH, dies, policy)
        w.restart()
    result = w.steps().step(sid, "tool", "fetch", HASH, dies, policy)
    assert runs == 3
    assert (result.outcome, result.code, result.attempts) == ("error", "interrupted", 3)
    failed = [(e.payload["attempt"], e.payload["retried"], e.payload["duration_ms"])
              for e in w.store.read("o", run=RUN) if e.type == "step.failed"]  # fmt: skip
    assert failed == [(1, True, 5_000), (2, True, 30_000), (3, False, 30_000)]
    w.restart()
    assert meter(load_view(w.store, "o"), RUN).used["active_ms"] == 65_000
    again = w.steps().step(sid, "tool", "fetch", HASH, dies, policy)
    assert again.replayed and runs == 3  # the recorded error is returned, nothing runs


class Looping:
    """A run's code that crashes on every drive; ``progress`` saves one tool step first."""

    def __init__(self, w: World, *, progress: bool) -> None:
        self.w = w
        self.progress = progress
        self.drives = 0

    def __call__(self, run: RunSummary, steps: DurableStepExecutor) -> None:
        self.drives += 1
        if self.progress:
            sid = StepId(run.run, "gather", turn=1, call=self.drives)
            steps.step(sid, "tool", "fetch", HASH, lambda d: StepOutput({"n": 1}), StepPolicy())
        raise Crash("the test suite exhausts memory")


def _crash_and_recover(w: World, driver: Looping) -> tuple[str, ...]:
    w.restart()
    recover_on_startup(w.store, LOADED)
    try:
        return resume_owner(w.store, "o", driver, clock=w.clock, loaded=LOADED)
    except Crash:
        return ("crashed",)


def test_crash_loop_cap_from_resume_records(w: World) -> None:
    w.start()
    driver = Looping(w, progress=False)
    outcomes = [_crash_and_recover(w, driver) for _ in range(5)]
    assert outcomes == [("crashed",)] * 3 + [(), ()]
    assert driver.drives == 3  # the fourth resume ended the run instead of driving it
    decided = [e.payload for e in w.store.read("o") if e.type == "stop.decided"]
    assert [d["verdict"] for d in decided] == ["continue"] * 3 + ["stop"]
    assert decided[-1]["reason"] == "restart_limit"
    assert decided[-1]["counts"] == {"resumes": 3, "restart_limit": 3}
    summary = rebuild(RUNS, load_view(w.store, "o"))[RUN]
    assert summary.outcome == "failed"
    ended = [e.payload for e in w.store.read("o") if e.type == "run.ended"]
    assert ended == [{"outcome": "failed", "reason": "restart_limit"}]


def test_progress_between_crashes_keeps_the_run_going(w: World) -> None:
    w.start()
    driver = Looping(w, progress=True)
    outcomes = [_crash_and_recover(w, driver) for _ in range(6)]
    assert outcomes == [("crashed",)] * 6 and driver.drives == 6
    assert resumes_since_progress(load_view(w.store, "o"), RUN) == 0  # progress after each


def test_a_held_runs_restarts_never_count(w: World) -> None:
    w.start()
    held = NewEvent("interrupt.raised", {"interrupt": "I-0001"}, run=RUN)
    w.store.append("o", "U2", (held,))
    driver = Looping(w, progress=False)
    outcomes = [_crash_and_recover(w, driver) for _ in range(5)]
    assert outcomes == [()] * 5 and driver.drives == 0
    assert resumes_since_progress(load_view(w.store, "o"), RUN) == 0
    assert not [e for e in w.store.read("o") if e.type in ("stop.decided", "run.ended")]
