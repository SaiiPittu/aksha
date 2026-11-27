"""The durable step executor on a SQLite store (ADR-P02 D1, D2, D4, D9).

A crash is a ``Crash`` (a ``BaseException``) raised inside the work, after which the store
is closed and opened again, as a restarted process would.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.sandbox.process import ProcessGroupUnit
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.budgets.types import Backoff
from aksha.runtime.cancellation.command import stop
from aksha.runtime.cancellation.types import KillPolicy
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution import facts
from aksha.runtime.execution.errors import (
    EffectNeedsOutbox,
    StepMismatch,
    TransientStepError,
)
from aksha.runtime.execution.executor import STEP_EXCEPTION, StepExecutor
from aksha.runtime.execution.types import Deadline, StepId, StepOutput, StepPolicy
from aksha.runtime.policy.events import args_hash
from aksha.runtime.storage.ports import EventStore
from tests.support.events import START, artifact_row, run_started
from tests.support.steps import HASH, Crash, Script, log, types

S1 = StepId("R-0001", "a", turn=1, call=1)
S2 = StepId("R-0001", "a", turn=1, call=2)
ONCE = StepPolicy(semantics="at_most_once")
FAST = StepPolicy(backoff=Backoff(base_ms=500, factor=2, cap_ms=4_000))


class Harness:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.clock = FakeClock(START)
        self.store: EventStore = open_store(path, self.clock, writer=True)
        self.store.append("o", "U1", (run_started(),))

    def executor(self) -> StepExecutor:
        return StepExecutor(self.store, "o", self.clock)

    def restart(self) -> StepExecutor:
        self.store.close()
        self.store = open_store(self.path, self.clock, writer=True)
        return self.executor()


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path / "aksha.sqlite")
    yield harness
    harness.store.close()


def test_step_is_recorded_before_and_after(h: Harness) -> None:
    script = Script()
    result = h.executor().step(S1, "tool", "fetch", HASH, script.ok("one"), StepPolicy())
    assert (result.outcome, result.value, result.attempts, result.replayed) == (
        "ok", {"label": "one"}, 1, False,
    )  # fmt: skip
    started, completed = h.store.read("o", after_seq=1)
    assert (started.type, started.step, started.payload["args_hash"]) == (
        "step.started", str(S1), HASH,
    )  # fmt: skip
    assert started.payload["semantics"] == "at_least_once"
    assert started.payload["tool"] == started.payload["name"] == "fetch"
    assert (completed.type, completed.payload["value"]) == ("step.completed", {"label": "one"})


def test_recorded_step_returns_its_result_without_running(h: Harness) -> None:
    script = Script()
    first = h.executor().step(S1, "model", "draft", HASH, script.ok("one"), StepPolicy())
    again = h.restart().step(S1, "model", "draft", HASH, script.ok("two"), StepPolicy())
    assert script.calls == ["one"]
    assert again.replayed and not first.replayed
    assert again.value == first.value == {"label": "one"}
    assert types(h.store).count("step.completed") == 1


@pytest.mark.parametrize(
    ("kind", "name", "digest"),
    [
        ("tool", "fetch", args_hash({"q": "other"})),
        ("tool", "send", HASH),
        ("model", "fetch", HASH),
    ],
)
def test_same_step_id_with_other_arguments_raises_step_mismatch(
    h: Harness, kind: str, name: str, digest: str
) -> None:
    script = Script()
    h.executor().step(S1, "tool", "fetch", HASH, script.ok("one"), StepPolicy())
    with pytest.raises(StepMismatch) as e:
        h.restart().step(S1, kind, name, digest, script.ok("two"), StepPolicy())  # type: ignore[arg-type]
    assert e.value.code == "step_mismatch"
    assert script.calls == ["one"]


def test_mismatch_is_raised_for_a_dangling_step_too(h: Harness) -> None:
    with pytest.raises(Crash):
        h.executor().step(S1, "tool", "fetch", HASH, Script().crash("x"), StepPolicy())
    with pytest.raises(StepMismatch):
        h.restart().step(S1, "tool", "fetch", args_hash({}), Script().ok("y"), StepPolicy())


def test_crash_leaves_the_step_dangling_and_at_least_once_runs_it_again(h: Harness) -> None:
    script = Script()
    ex = h.executor()
    ex.step(S1, "tool", "fetch", HASH, script.ok("one"), StepPolicy())
    with pytest.raises(Crash):
        ex.step(S2, "tool", "fetch", HASH, script.crash("two"), StepPolicy())
    assert types(h.store) == ["run.started", "step.started", "step.completed", "step.started"]
    ex = h.restart()
    first = ex.step(S1, "tool", "fetch", HASH, script.ok("one again"), StepPolicy())
    second = ex.step(S2, "tool", "fetch", HASH, script.ok("two again"), StepPolicy())
    assert script.calls == ["one", "two", "two again"]
    assert first.replayed and not second.replayed
    assert types(h.store).count("step.started") == 2  # the intent is never written twice


def test_at_most_once_commits_dispatch_alone_before_the_body(h: Harness) -> None:
    seen: list[list[str]] = []

    def work(deadline: Deadline) -> StepOutput:
        seen.append(types(h.store))
        return StepOutput({"sent": True})

    result = h.executor().step(S1, "tool", "send", HASH, work, ONCE)
    assert seen == [["run.started", "step.started", "step.dispatched"]]
    assert result.outcome == "ok"
    assert types(h.store)[-1] == "step.completed"


def test_dispatched_at_most_once_step_is_interrupted_never_rerun(h: Harness) -> None:
    script = Script()
    with pytest.raises(Crash):
        h.executor().step(S1, "tool", "send", HASH, script.crash("send"), ONCE)
    result = h.restart().step(S1, "tool", "send", HASH, script.ok("send again"), ONCE)
    assert script.calls == ["send"]
    assert (result.outcome, result.code, result.value, result.replayed) == (
        "interrupted", "outcome_unknown", None, False,
    )  # fmt: skip
    completed = log(h.store)[-1]
    assert completed[2] == {
        "outcome": "interrupted", "kind": "tool", "tool": "send", "attempts": 1,
        "code": "outcome_unknown", "on_unknown": "tell_model",
    }  # fmt: skip
    again = h.restart().step(S1, "tool", "send", HASH, script.ok("third"), ONCE)
    assert again.replayed and again.outcome == "interrupted"
    assert script.calls == ["send"]


def test_at_most_once_started_but_never_dispatched_runs(h: Harness) -> None:
    h.store.append("o", "U2a", (facts.step_started(S1, "tool", "send", HASH, ONCE),))
    script = Script()
    result = h.restart().step(S1, "tool", "send", HASH, script.ok("send"), ONCE)
    assert script.calls == ["send"]
    assert result.outcome == "ok"


def test_at_most_once_is_never_retried(h: Harness) -> None:
    script = Script()
    work = script.raises("send", TransientStepError(message="flaky"))
    result = h.executor().step(S1, "tool", "send", HASH, work, ONCE)
    assert script.calls == ["send"]
    assert (result.outcome, result.code) == ("error", "transient")
    assert types(h.store).count("step.dispatched") == 1


def test_at_most_once_timeout_is_outcome_unknown(h: Harness) -> None:
    release = threading.Event()

    def hangs(deadline: Deadline) -> StepOutput:
        release.wait(5)
        return StepOutput({"late": True})

    policy = StepPolicy(semantics="at_most_once", timeout_ms=30, on_unknown="interrupt")
    ex = h.executor()
    result = ex.step(S1, "tool", "deploy", HASH, hangs, policy)
    assert (result.outcome, result.code) == ("interrupted", "outcome_unknown")
    assert log(h.store)[-1][2]["on_unknown"] == "interrupt"
    assert "step.failed" not in types(h.store)
    release.set()


def test_a_reconcile_step_outside_the_outbox_is_refused(h: Harness) -> None:
    script = Script()
    policy = StepPolicy(semantics="reconcile")
    with pytest.raises(EffectNeedsOutbox) as e:
        h.executor().step(S1, "tool", "file", HASH, script.ok("file"), policy)
    assert e.value.code == "effect_needs_outbox"
    assert script.calls == []
    assert "step.started" not in types(h.store)


def test_transient_failures_retry_with_backoff_on_the_injected_clock(h: Harness) -> None:
    script = Script()
    work = script.raises("f", TransientStepError(), TransientStepError())
    result = h.executor().step(S1, "tool", "fetch", HASH, work, FAST)
    assert (result.outcome, result.attempts) == ("ok", 3)
    failed = [p for t, _, p in log(h.store) if t == "step.failed"]
    assert failed == [
        {"attempt": 1, "code": "transient", "retried": True, "backoff_ms": 500, "duration_ms": 0},
        {"attempt": 2, "code": "transient", "retried": True, "backoff_ms": 1_000, "duration_ms": 0},
    ]
    assert (h.clock.now() - START).total_seconds() == 1.5


def test_retries_used_up_end_in_an_error(h: Harness) -> None:
    script = Script()
    work = script.raises("f", *(TransientStepError() for _ in range(5)))
    result = h.executor().step(S1, "tool", "fetch", HASH, work, FAST)
    assert (result.outcome, result.code, result.attempts) == ("error", "transient", 3)
    assert len(script.calls) == 3
    retried = [p["retried"] for t, _, p in log(h.store) if t == "step.failed"]
    assert retried == [True, True, False]


def test_the_backoff_is_at_least_what_the_failure_asked_for(h: Harness) -> None:
    script = Script()
    work = script.raises("f", TransientStepError(retry_after_ms=7_000), TransientStepError())
    result = h.executor().step(S1, "tool", "fetch", HASH, work, FAST)
    assert (result.outcome, result.attempts) == ("ok", 3)
    backoffs = [p["backoff_ms"] for t, _, p in log(h.store) if t == "step.failed"]
    assert backoffs == [7_000, 1_000]  # the Retry-After, then the policy's own backoff
    assert (h.clock.now() - START).total_seconds() == 8.0


def test_no_retry_waits_past_the_step_s_wait_cap(h: Harness) -> None:
    script = Script()
    work = script.raises("f", *(TransientStepError() for _ in range(9)))
    policy = StepPolicy(max_retries=8, backoff=Backoff(base_ms=500, factor=2, cap_ms=4_000),
                        wait_cap_ms=2_000)  # fmt: skip
    result = h.executor().step(S1, "tool", "fetch", HASH, work, policy)
    assert (result.outcome, result.code, result.attempts) == ("error", "transient", 3)
    failed = [(p["retried"], p["backoff_ms"]) for t, _, p in log(h.store) if t == "step.failed"]
    assert failed == [(True, 500), (True, 1_000), (False, 0)]  # 500 + 1000 + 2000 > 2000
    assert (h.clock.now() - START).total_seconds() == 1.5


def test_an_unlisted_code_is_not_retried(h: Harness) -> None:
    script = Script()
    work = script.raises("f", TransientStepError("rate_limited"))
    result = h.executor().step(S1, "tool", "fetch", HASH, work, FAST)
    assert (result.outcome, result.code, script.calls) == ("error", "rate_limited", ["f"])


def test_an_ordinary_exception_is_a_recorded_error_not_a_crash(h: Harness) -> None:
    script = Script()
    work = script.raises("f", RuntimeError("bug"))
    result = h.executor().step(S1, "tool", "fetch", HASH, work, FAST)
    assert (result.outcome, result.code) == ("error", STEP_EXCEPTION)
    assert script.calls == ["f"]


def test_work_that_returns_no_step_output_is_an_error(h: Harness) -> None:
    result = h.executor().step(S1, "tool", "f", HASH, lambda d: {"x": 1}, FAST)  # type: ignore[arg-type,return-value]
    assert (result.outcome, result.code) == ("error", STEP_EXCEPTION)


def test_an_error_output_is_recorded_and_not_retried(h: Harness) -> None:
    calls: list[int] = []

    def work(deadline: Deadline) -> StepOutput:
        calls.append(1)
        return StepOutput({"message": "no such page"}, outcome="error", code="not_found")

    result = h.executor().step(S1, "tool", "fetch", HASH, work, FAST)
    assert (result.outcome, result.code, result.value) == (
        "error", "not_found", {"message": "no such page"},
    )  # fmt: skip
    assert calls == [1]
    assert "step.failed" not in types(h.store)


def test_retry_count_survives_a_restart(h: Harness) -> None:
    script = Script()
    work = script.raises("f", TransientStepError(), Crash("during attempt 2"))
    with pytest.raises(Crash):
        h.executor().step(S1, "tool", "fetch", HASH, work, FAST)
    again = script.raises("g", TransientStepError(), TransientStepError(), TransientStepError())
    result = h.restart().step(S1, "tool", "fetch", HASH, again, FAST)
    # attempt 1 failed and was recorded; attempt 2 crashed unrecorded and runs again as 2
    failed = [(p["attempt"], p["retried"]) for t, _, p in log(h.store) if t == "step.failed"]
    assert failed == [(1, True), (2, True), (3, False)]
    assert (result.outcome, result.attempts) == ("error", 3)


def test_abandoned_attempt_late_result_discarded(h: Harness) -> None:
    release = threading.Event()
    returned = threading.Event()
    attempts: list[int] = []

    def work(deadline: Deadline) -> StepOutput:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            release.wait(5)
            returned.set()
            return StepOutput({"from": "attempt 1"})
        return StepOutput({"from": "attempt 2"})

    ex = h.executor()
    policy = StepPolicy(timeout_ms=40, backoff=Backoff(base_ms=10))
    result = ex.step(S1, "tool", "fetch", HASH, work, policy)
    assert result.value == {"from": "attempt 2"}
    release.set()
    assert returned.wait(5)
    ex.step(S2, "tool", "next", HASH, lambda d: StepOutput({"n": 1}), policy)  # drains first
    kinds = [(t, p.get("attempt"), p.get("code")) for t, s, p in log(h.store) if s == str(S1)]
    assert kinds == [
        ("step.started", None, None),
        ("step.failed", 1, "timeout"),
        ("step.completed", None, None),
        ("attempt.discarded", 1, None),
    ]
    discarded = next(p for t, _, p in log(h.store) if t == "attempt.discarded")
    assert discarded["result_hash"] == facts.value_hash({"from": "attempt 1"})
    assert ex.drain_late() == 0


def test_output_facts_commit_with_the_step_result(h: Harness) -> None:
    row = artifact_row()
    saved = NewEvent(
        "artifact.saved",
        {"ref": row.ref, "kind": row.kind, "key": row.key, "version": 1,
         "content_hash": row.content_hash},
    )  # fmt: skip
    out = StepOutput({"ref": row.ref}, events=(saved,), artifacts=(row,))
    h.executor().step(S1, "tool", "save_artifact", HASH, lambda d: out, StepPolicy())
    *_, a, b = h.store.read("o")
    assert (a.type, b.type) == ("artifact.saved", "step.completed")
    assert [r.ref for r in h.store.read_artifacts("o")] == [row.ref]


def test_gate_fields_ride_on_step_started(h: Harness) -> None:
    extra = {"route": "dispatch", "position": 0}
    h.executor().step(S1, "tool", "fetch", HASH, Script().ok("x"), StepPolicy(), extra=extra)
    started = log(h.store)[1][2]
    assert (started["route"], started["position"]) == ("dispatch", 0)


def _scripted_run(path: Path) -> list[tuple[str, str | None, str | None, str, object]]:
    clock = FakeClock(START)
    store = open_store(path, clock, writer=True)
    try:
        store.append("o", "U1", (run_started(),))
        ex = StepExecutor(store, "o", clock)
        script = Script()
        ex.step(S1, "model", "draft", HASH, script.ok("m"), FAST)
        ex.step(S2, "tool", "fetch", HASH, script.raises("t", TransientStepError()), FAST)
        ex.step(S2.with_turn(2).with_call(1), "tool", "send", HASH, script.ok("s"), ONCE)
        return [(e.type, e.run, e.step, e.at, dict(e.payload)) for e in store.read("o")]
    finally:
        store.close()


def test_two_fake_runs_write_identical_logs(tmp_path: Path) -> None:
    first = _scripted_run(tmp_path / "one.sqlite")
    second = _scripted_run(tmp_path / "two.sqlite")
    assert first == second
    assert len(first) == 9


def test_attempt_durations_are_read_on_the_injected_clock(h: Harness) -> None:
    def slow(deadline: Deadline) -> StepOutput:
        h.clock.advance(2.5)  # the work takes 2.5 s of injected time
        return StepOutput({"ok": True})

    h.executor().step(S1, "tool", "fetch", HASH, slow, FAST)
    done = next(p for t, _, p in log(h.store) if t == "step.completed")
    assert done["duration_ms"] == 2_500


def test_a_timed_out_attempt_records_its_timeout_as_its_duration(h: Harness) -> None:
    gate = threading.Event()
    policy = StepPolicy(timeout_ms=30, max_retries=0)
    h.executor().step(S1, "tool", "fetch", HASH, lambda d: (gate.wait(5), StepOutput())[1], policy)
    gate.set()
    failed = next(p for t, _, p in log(h.store) if t == "step.failed")
    assert (failed["code"], failed["duration_ms"]) == ("timeout", 30)


def test_timeout_real_time(h: Harness) -> None:
    gate = threading.Event()
    policy = StepPolicy(timeout_ms=80, max_retries=0)
    began = h.clock.mono_ms()
    result = h.executor().step(
        S1, "tool", "fetch", HASH, lambda d: (gate.wait(5), StepOutput())[1], policy
    )
    waited = h.clock.mono_ms() - began
    gate.set()
    assert (result.outcome, result.code) == ("error", "timeout")
    assert 80 <= waited < 2_000  # abandoned at the deadline, not after the body returned


def test_model_usage_is_recorded_on_the_step_result(h: Harness) -> None:
    use = {"input_tokens": 12, "output_tokens": 3, "cost_micro_usd": 40}
    work = lambda d: StepOutput({"text": "hi"}, usage=use)  # noqa: E731
    h.executor().step(S1, "model", "draft", HASH, work, StepPolicy())
    done = next(p for t, _, p in log(h.store) if t == "step.completed")
    assert done["usage"] == use


def test_step_output_refuses_unknown_or_negative_usage() -> None:
    with pytest.raises(ValueError, match="usage"):
        StepOutput(usage={"seconds": 1})
    with pytest.raises(ValueError, match="usage"):
        StepOutput(usage={"input_tokens": -1})


# --- ADR-P18 D5 and D6: a stop between steps, and the kill boundary ---------------------

S3 = StepId("R-0001", "a", turn=1, call=3)
QUICK = KillPolicy(grace_seconds=0.3, reap_seconds=5.0)
SLEEPS = "import time; print('ready', flush=True); time.sleep(60)"
IGNORES_TERM = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('ready', flush=True); time.sleep(60)"
)


def test_a_new_step_after_a_stop_request_is_skipped_and_never_runs(h: Harness) -> None:
    script = Script()
    stop(h.store, "o", "R-0001", command_id="c-1")
    result = h.executor().step(S1, "tool", "fetch", HASH, script.ok("one"), StepPolicy())
    assert (result.outcome, result.code, result.attempts) == ("skipped", "run_stopped", 0)
    assert script.calls == []
    assert [(t, s, p) for t, s, p in log(h.store) if s == str(S1)] == [
        ("step.completed", str(S1),
         {"outcome": "skipped", "kind": "tool", "tool": "fetch", "attempts": 0,
          "code": "run_stopped"}),
    ]  # fmt: skip


def test_a_recorded_step_still_replays_after_a_stop_request(h: Harness) -> None:
    h.executor().step(S1, "tool", "fetch", HASH, Script().ok("one"), StepPolicy())
    stop(h.store, "o", "R-0001", command_id="c-1")
    again = h.executor().step(S1, "tool", "fetch", HASH, Script().ok("one"), StepPolicy())
    assert (again.outcome, again.replayed) == ("ok", True)


def test_a_running_step_without_a_unit_finishes_and_the_next_is_skipped(h: Harness) -> None:
    def work(deadline: Deadline) -> StepOutput:
        stop(h.store, "o", "R-0001", command_id="c-1")  # the person stops mid-call
        return StepOutput({"done": True})

    steps = h.executor()
    first = steps.step(S1, "tool", "fetch", HASH, work, StepPolicy())
    second = steps.step(S2, "tool", "fetch", HASH, Script().ok("two"), StepPolicy())
    assert (first.outcome, first.value) == ("ok", {"done": True})
    assert (second.outcome, second.code) == ("skipped", "run_stopped")


class Child:
    """A tool body running ``source`` in its own process group, and a person who stops it.

    The person's thread commits the stop once the child has printed ``ready``, so the stop
    lands while the step is running.
    """

    def __init__(self, h: Harness, source: str, *, stop_when_ready: bool = True) -> None:
        self.unit = ProcessGroupUnit([sys.executable, "-c", source])
        self.ready = threading.Event()
        self.person = threading.Thread(target=self._stop, args=(h,))
        if stop_when_ready:
            self.person.start()

    def _stop(self, h: Harness) -> None:
        assert self.ready.wait(10)
        stop(h.store, "o", "R-0001", command_id="c-1")

    def work(self, deadline: Deadline) -> StepOutput:
        self.unit.start()
        self.unit.ready_line()
        self.ready.set()
        self.unit.wait(60)
        return StepOutput({"returncode": self.unit.returncode})


def test_a_stop_cancels_a_killable_step_with_no_effects(h: Harness) -> None:
    child = Child(h, SLEEPS)
    steps = StepExecutor(h.store, "o", h.clock, kill=QUICK)
    began = h.clock.mono_ms()
    result = steps.step(S1, "tool", "run_tests", HASH, child.work, StepPolicy(), unit=child.unit)
    child.person.join()
    assert (result.outcome, result.code) == ("cancelled", "run_stopped")
    assert h.clock.mono_ms() - began < 10_000
    done = next(p for t, s, p in log(h.store) if t == "step.completed" and s == str(S1))
    assert (done["outcome"], done["kill"]) == ("cancelled", "settled")
    assert child.unit.settled()


def test_kill_boundary_records_effects_unknown(h: Harness) -> None:
    child = Child(h, IGNORES_TERM)
    steps = StepExecutor(h.store, "o", h.clock, kill=QUICK)
    result = steps.step(S1, "tool", "push", HASH, child.work, ONCE, unit=child.unit)
    child.person.join()
    assert (result.outcome, result.code) == ("effects_unknown", "run_stopped")
    done = next(p for t, s, p in log(h.store) if t == "step.completed" and s == str(S1))
    assert (done["outcome"], done["kill"]) == ("effects_unknown", "killed")
    returncode = child.unit.returncode
    assert returncode is not None and returncode < 0
    after = steps.step(S2, "tool", "fetch", HASH, Script().ok("two"), StepPolicy())
    assert after.outcome == "skipped"


def test_a_killable_step_with_no_stop_finishes_normally(h: Harness) -> None:
    child = Child(h, "print('ready', flush=True)", stop_when_ready=False)
    steps = StepExecutor(h.store, "o", h.clock, kill=QUICK)
    result = steps.step(S1, "tool", "run_tests", HASH, child.work, StepPolicy(), unit=child.unit)
    assert (result.outcome, result.value) == ("ok", {"returncode": 0})


# --- review round: atomic carried facts, stops on dangling steps, exec timeouts --------


class CrashOnResult:
    """Wraps a store: the append that carries a ``step.completed`` dies before it commits."""

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def __getattr__(self, name: str) -> object:
        return getattr(self.store, name)

    def append(self, owner: str, boundary: str, events: object, artifacts: object = ()) -> object:
        kinds = tuple(e.type for e in events)  # type: ignore[attr-defined]
        self.calls.append((boundary, kinds))
        if "step.completed" in kinds:
            raise Crash("killed before the result commits")
        return self.store.append(owner, boundary, events, artifacts)  # type: ignore[arg-type]


def test_carried_facts_and_the_result_are_one_transaction(h: Harness) -> None:
    """ADR-P02 interface item 5 (U2): a kill before the result leaves no save behind."""
    row = artifact_row()
    saved = NewEvent(
        "artifact.saved",
        {"ref": row.ref, "kind": row.kind, "key": row.key, "version": 1,
         "content_hash": row.content_hash},
    )  # fmt: skip
    script = Script()

    def work(deadline: Deadline) -> StepOutput:
        script.calls.append("save")
        return StepOutput({"ref": row.ref}, events=(saved,), artifacts=(row,))

    crashing = CrashOnResult(h.store)
    with pytest.raises(Crash):
        StepExecutor(crashing, "o", h.clock).step(  # type: ignore[arg-type]
            S1, "tool", "save_artifact", HASH, work, StepPolicy()
        )
    assert crashing.calls[-1] == ("U2", ("artifact.saved", "step.completed"))
    assert "artifact.saved" not in types(h.store)  # nothing of the result committed
    assert list(h.store.read_artifacts("o")) == []
    result = h.restart().step(S1, "tool", "save_artifact", HASH, work, StepPolicy())
    assert (result.outcome, script.calls) == ("ok", ["save", "save"])
    assert types(h.store).count("artifact.saved") == 1
    assert [r.ref for r in h.store.read_artifacts("o")] == [row.ref]


def test_a_dangling_step_of_a_stopped_run_starts_no_new_attempt(h: Harness) -> None:
    script = Script()
    with pytest.raises(Crash):
        h.executor().step(S1, "tool", "fetch", HASH, script.crash("one"), StepPolicy())
    ex = h.restart()
    stop(h.store, "o", "R-0001", command_id="c-1")  # committed while the resumed run drives
    result = ex.step(S1, "tool", "fetch", HASH, script.ok("two"), StepPolicy())
    assert (result.outcome, result.code) == ("cancelled", "run_stopped")
    assert script.calls == ["one"]  # the crashed attempt only


def test_an_undispatched_at_most_once_step_of_a_stopped_run_is_skipped(h: Harness) -> None:
    script = Script()
    h.store.append("o", "U2a", (facts.step_started(S1, "tool", "send", HASH, ONCE),))
    stop(h.store, "o", "R-0001", command_id="c-1")
    result = h.restart().step(S1, "tool", "send", HASH, script.ok("send"), ONCE)
    assert (result.outcome, result.code) == ("skipped", "run_stopped")
    assert script.calls == []
    assert "step.dispatched" not in types(h.store)


def _unit_work(unit: ProcessGroupUnit) -> object:
    def work(deadline: Deadline) -> StepOutput:
        unit.start()
        unit.wait(60)
        return StepOutput({"returncode": unit.returncode})

    return work


def test_an_exec_timeout_kills_the_process_group_and_is_not_retried(h: Harness) -> None:
    """ADR-P14 D4, D5: the unit is ended at the deadline, and an exec timeout never repeats."""
    unit = ProcessGroupUnit(["sleep", "30"])
    steps = StepExecutor(h.store, "o", h.clock, kill=QUICK)
    began = h.clock.mono_ms()
    policy = StepPolicy(timeout_ms=300)  # ``timeout`` is in the default retry_on
    result = steps.step(S1, "tool", "run_tests", HASH, _unit_work(unit), policy, unit=unit)  # type: ignore[arg-type]
    assert (result.outcome, result.code, result.attempts) == ("error", "timeout", 1)
    assert unit.settled()  # the child is gone, not left running
    assert unit.returncode is not None and unit.returncode < 0
    assert h.clock.mono_ms() - began < 10_000
    mine = [(t, p) for t, s, p in log(h.store) if s == str(S1)]
    assert [t for t, _ in mine if t != "attempt.discarded"] == [
        "step.started", "step.failed", "step.completed",
    ]  # fmt: skip
    failed = next(p for t, p in mine if t == "step.failed")
    assert (failed["code"], failed["retried"]) == ("timeout", False)
    done = next(p for t, p in mine if t == "step.completed")
    assert done["kill"] in ("settled", "killed")


def test_an_at_most_once_exec_timeout_is_outcome_unknown(h: Harness) -> None:
    unit = ProcessGroupUnit(["sleep", "30"])
    steps = StepExecutor(h.store, "o", h.clock, kill=QUICK)
    policy = StepPolicy(timeout_ms=300, semantics="at_most_once", on_unknown="interrupt")
    result = steps.step(S1, "tool", "push", HASH, _unit_work(unit), policy, unit=unit)  # type: ignore[arg-type]
    assert (result.outcome, result.code) == ("interrupted", "outcome_unknown")
    assert unit.settled()
    done = next(p for t, s, p in log(h.store) if t == "step.completed" and s == str(S1))
    assert done["on_unknown"] == "interrupt" and "kill" in done


def test_a_unit_step_with_a_transient_failure_is_not_retried(h: Harness) -> None:
    unit = ProcessGroupUnit(["true"])
    script = Script()
    work = script.raises("exec", TransientStepError())
    steps = StepExecutor(h.store, "o", h.clock, kill=QUICK)
    result = steps.step(S1, "tool", "run", HASH, work, StepPolicy(), unit=unit)
    assert (result.outcome, result.code) == ("error", "transient")
    assert script.calls == ["exec"]  # one unit is one process: no second attempt on it


def test_effects_unknown_tells_the_model_or_a_person(h: Harness) -> None:
    child = Child(h, IGNORES_TERM)
    steps = StepExecutor(h.store, "o", h.clock, kill=QUICK)
    policy = StepPolicy(semantics="at_most_once", on_unknown="interrupt")
    steps.step(S1, "tool", "push", HASH, child.work, policy, unit=child.unit)
    child.person.join()
    done = next(p for t, s, p in log(h.store) if t == "step.completed" and s == str(S1))
    assert (done["outcome"], done["on_unknown"]) == ("effects_unknown", "interrupt")


# --- the agent loop's records: a refusal, a skip, a recorded result (ADR-P06 D5) ---------


def _refusal(step: StepId) -> NewEvent:
    from aksha.runtime.policy.events import decision_event
    from aksha.runtime.policy.types import Refuse
    from aksha.runtime.tools.types import ToolCall

    refused = Refuse("unknown_tool", 1, "no tool named 'nope'", counted=True)
    sid = str(step)
    return decision_event(refused, ToolCall("nope", {}, 1), run=step.run,
                          instance=str(step.instance), step=sid)  # fmt: skip


def test_a_refusal_is_the_steps_one_fact_and_is_recorded_once(h: Harness) -> None:
    ex = h.executor()
    assert ex.recorded(S1) is None
    first = ex.refuse(S1, _refusal(S1))
    assert (first.outcome, first.code, first.replayed) == ("refused", "unknown_tool", False)
    again = h.restart().refuse(S1, _refusal(S1))
    assert again.replayed and again.outcome == "refused"
    assert [t for t, s, _ in log(h.store) if s == str(S1)] == ["step.completed"]


def test_refuse_takes_only_a_refusal_of_its_own_step(h: Harness) -> None:
    ex = h.executor()
    with pytest.raises(ValueError, match="names step"):
        ex.refuse(S2, _refusal(S1))
    ok = facts.step_completed(S1, "tool", "x", "ok", attempts=1)
    with pytest.raises(ValueError, match="refused"):
        ex.refuse(S1, ok)


def test_a_skip_never_runs_and_is_recorded_once(h: Harness) -> None:
    ex = h.executor()
    skipped = ex.skip(S2, "tool", "send", "after_approval")
    assert (skipped.outcome, skipped.code, skipped.attempts) == ("skipped", "after_approval", 0)
    assert h.restart().skip(S2, "tool", "send", "after_approval").replayed
    recorded = h.executor().recorded(S2)
    assert recorded is not None and recorded.code == "after_approval"


def test_a_started_step_is_never_skipped(h: Harness) -> None:
    ex = h.executor()
    with pytest.raises(Crash):
        ex.step(S1, "tool", "t", HASH, Script().crash("t"), StepPolicy())
    with pytest.raises(StepMismatch, match="never skipped"):
        h.restart().skip(S1, "tool", "t", "after_finish")
