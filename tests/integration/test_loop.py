"""The agent loop end to end (ADR-P06 Tests), on SQLite and the durable step executor.

Each test drives the ``coder`` bench's run through real stop-policy evaluation: a turn's
model step and call positions are recorded through the executor, every turn end is
judged, and a kill is a ``Crash`` (a ``BaseException``) that leaves the log as a dying
process would. A restart opens the store again and resumes through recovery.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.budgets.meter import meter
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.types import StepId
from aksha.runtime.interrupts.approvals import ApprovalTool, approval_authority, approvals
from aksha.runtime.interrupts.commands import approve
from aksha.runtime.loop import drive as drive_module
from aksha.runtime.loop.classify import recorded_turn_end
from aksha.runtime.loop.drive import control_point
from aksha.runtime.loop.turn import run_turn as real_run_turn
from aksha.runtime.loop.types import TurnEnd
from aksha.runtime.models.types import GenerationRequest, GenerationResult
from aksha.runtime.outbox.worker import OutboxWorker
from aksha.runtime.stop_policy.commit import commit_stop_decision
from aksha.runtime.stop_policy.core import MALFORMED_REASON
from tests.support.loop import (
    EDIT,
    FINISH,
    OWNER,
    RUN,
    SAVE_NOTE,
    Bench,
    Bodies,
    Crash,
    Recorder,
    call,
    finish_reply,
    tools_reply,
)
from tests.support.systems import Ledger

T01 = f"{RUN}/edit/t01"
SEND = call("send", document="doc:1@v1", recipient_id="P-1")


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Any]:
    benches: list[Bench] = []

    def build(model: Recorder, bodies: Bodies | None = None, **kw: Any) -> Bench:
        b = Bench(tmp_path / f"aksha-{len(benches)}.sqlite", model, bodies, **kw)
        b.start()
        benches.append(b)
        return b

    yield build
    for b in benches:
        b.close()


def _counted_turns(monkeypatch: pytest.MonkeyPatch) -> list[TurnEnd]:
    """Wrap the driver's ``run_turn`` so a test sees every turn it ran."""
    ran: list[TurnEnd] = []
    real = real_run_turn

    def counted(*args: Any, **kw: Any) -> TurnEnd:
        end = real(*args, **kw)
        ran.append(end)
        return end

    monkeypatch.setattr(drive_module, "run_turn", counted)
    return ran


def test_a_run_reads_saves_and_finishes_complete(make: Any) -> None:
    model = Recorder([tools_reply(call("read_file", path="a.py")), finish_reply()])
    b = make(model, Bodies(files={"a.py": "x = 1"}))
    result = b.drive()
    assert (result.state, result.outcome) == ("ended", "complete")
    assert [e.kind for e in result.turns] == ["tool_results", "final_answer"]
    assert model.steps == [T01, f"{RUN}/edit/t02"]
    assert b.bodies.calls == ["read_file:a.py"]
    assert [e.payload["ref"] for e in b.events("artifact.saved")] == ["coder.note@v1"]


def test_tool_result_changes_next_call(make: Any) -> None:
    """Brief L146: two runs that differ in one tool result send different next requests."""
    runs = []
    for text in ("x = 1", "x = 2"):
        model = Recorder([tools_reply(call("read_file", path="a.py")), finish_reply()])
        make(model, Bodies(files={"a.py": text})).drive()
        runs.append(model.requests)
    first, second = runs
    assert first[0].context_hash == second[0].context_hash
    assert first[1].context_hash != second[1].context_hash
    tool_texts = [m.text or "" for m in second[1].messages if m.role == "tool"]
    assert any("x = 2" in t for t in tool_texts)


def test_runner_writes_no_event_of_its_own(make: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every event a turn writes carries a step ID of that turn, from the step executor."""
    spans: list[tuple[int, int, TurnEnd]] = []
    real = real_run_turn

    def watched(setup: Any, executor: Any, ref: Any, turn: int) -> TurnEnd:
        before = setup.view().seq_read
        end = real(setup, executor, ref, turn)
        spans.append((before, setup.view().seq_read, end))
        return end

    monkeypatch.setattr(drive_module, "run_turn", watched)
    model = Recorder([tools_reply(call("read_file", path="a.py"), call("nope")), finish_reply()])
    b = make(model)
    assert b.drive().outcome == "complete"
    assert len(spans) == 2
    for before, after, end in spans:
        prefix = str(StepId(RUN, end.instance, turn=end.turn))
        written = [e for e in b.view().events if before < e.seq <= after]
        assert written and end.last_seq == after
        assert all(e.step == prefix or str(e.step).startswith(prefix + "/") for e in written)


def test_positions_after_approval_are_skipped(make: Any) -> None:
    """Patent scenario: a send asks for approval; the call listed after it never runs."""
    model = Recorder([tools_reply(SEND, call("run_tests"))])
    b = make(model)
    result = b.drive()
    assert result.state == "held"
    assert [e.kind for e in result.turns] == ["approval"]
    assert b.bodies.calls == []
    skipped = b.completed(f"{T01}/c02").payload
    assert (skipped["outcome"], skipped["code"]) == ("skipped", "after_approval")
    assert [e.payload["approval"] for e in b.events("approval.requested")] == ["A-0001"]
    raised = b.events("interrupt.raised")
    assert [(e.payload["kind"], e.step) for e in raised] == [("approval", f"{T01}/c01")]
    assert b.decisions()[-1].payload["verdict"] == "hold"


def test_a_held_run_writes_nothing_when_driven_again(make: Any) -> None:
    b = make(Recorder([tools_reply(SEND)]))
    assert b.drive().state == "held"
    before = b.view().seq_read
    assert b.drive().state == "held"
    assert b.view().seq_read == before


def test_an_approved_send_runs_through_the_outbox(make: Any) -> None:
    model = Recorder([tools_reply(SEND), tools_reply(SEND), finish_reply()])
    b = make(model)
    mail = Ledger("mail")
    spec = b.setup.registry.get("send").spec
    authority = approval_authority(
        {"send": ApprovalTool(spec)}, lambda entry: b.setup.definition_hash
    )
    worker = OutboxWorker(b.store, OWNER, b.clock, {"mail": mail}, authority=authority)
    b.executor = StepExecutor(b.store, OWNER, b.clock, outbox=worker)
    assert b.drive().state == "held"
    a = approvals(b.view())["A-0001"]
    approve(b.store, OWNER, "A-0001", a.signature, by="reviewer")
    result = b.drive()
    assert (result.state, result.outcome) == ("ended", "complete")
    assert len(mail.rows) == 1 and b.bodies.calls == []
    sent = b.completed(f"{RUN}/edit/t02/c01").payload
    assert sent["outcome"] == "ok"
    assert approvals(b.view())["A-0001"].state == "used"


def test_ask_raises_an_interrupt_and_holds(make: Any) -> None:
    b = make(Recorder([tools_reply(call("ask", question="Which file?"), call("run_tests"))]))
    result = b.drive()
    assert result.state == "held" and result.turns[-1].kind == "interrupt"
    (raised,) = b.events("interrupt.raised")
    assert (raised.payload["interrupt"], raised.payload["kind"]) == ("I-0001", "input")
    assert b.completed(f"{T01}/c02").payload["code"] == "after_interrupt"
    assert b.bodies.calls == []


def test_an_ask_that_cannot_be_raised_is_refused_to_the_model_not_raised(make: Any) -> None:
    """An ask the model got wrong (two questions with one ID) comes back as a refusal it can
    correct, never a step exception that ends the run."""
    dup = [{"id": "q", "text": "Which file?"}, {"id": "q", "text": "Which test?"}]
    b = make(Recorder([tools_reply(call("ask", questions=dup)), finish_reply()]))
    assert b.drive().outcome == "complete"
    refused = b.completed(f"{T01}/c01").payload
    assert (refused["outcome"], refused["code"]) == ("error", "schema")
    assert b.events("interrupt.raised") == []


def test_only_continue_starts_a_turn(make: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _counted_turns(monkeypatch)
    model = Recorder([
        GenerationResult("I think it is done.", (), "text"),  # vetoed: no note yet
        tools_reply(call("run_tests")),
        finish_reply(),
    ])  # fmt: skip
    b = make(model)
    assert b.drive().outcome == "complete"
    continues = [d for d in b.decisions() if d.payload["verdict"] == "continue"]
    assert len(ran) == len(continues) == 3
    assert [d.payload["turn"] for d in continues] == [1, 2, 3]
    assert [e.kind for e in ran] == ["final_answer", "tool_results", "final_answer"]


def test_resume_mid_turn_replays_recorded_positions(make: Any) -> None:
    """Kill inside c02 of a three-call turn: on restart only c02 and c03 run."""
    reads = tuple(call("read_file", n, path=p) for n, p in enumerate(("a", "b", "c"), 1))
    model = Recorder([tools_reply(*reads), finish_reply()])
    b = make(model, Bodies(crash_on=("read_file", "b")))
    with pytest.raises(Crash):
        b.drive()
    assert model.steps == [T01]
    b.restart()
    assert b.resume() == (RUN,)
    assert model.steps == [T01, f"{RUN}/edit/t02"]  # t01's reply came from the record
    assert b.bodies.calls == ["read_file:a", "read_file:b", "read_file:b", "read_file:c"]
    assert b.ended()["outcome"] == "complete"


def test_resume_with_changed_context_fails(make: Any) -> None:
    reads = (call("read_file", 1, path="a"), call("read_file", 2, path="b"))
    model = Recorder([tools_reply(*reads)])
    b = make(model, Bodies(crash_on=("read_file", "b")))
    with pytest.raises(Crash):
        b.drive()
    b.restart(instructions="You edit other code.")
    b.resume()
    assert (b.ended()["outcome"], b.ended()["reason"]) == ("failed", "step_mismatch")
    assert model.steps == [T01] and b.bodies.calls == ["read_file:a", "read_file:b"]


def test_length_reply_is_malformed(make: Any) -> None:
    cut = GenerationResult("I will run the", (call("run_tests"),), "length")
    model = Recorder([cut, finish_reply()])
    b = make(model)
    result = b.drive()
    assert [e.kind for e in result.turns] == ["malformed", "final_answer"]
    assert b.bodies.calls == []
    assert b.store.by_dedup_key(OWNER, f"sstart:{T01}/c01") is None
    second = b.decisions()[1].payload
    assert (second["candidate"], second["verdict"]) == ("malformed", "continue")
    assert second["reason"] == MALFORMED_REASON


def test_unknown_tool_refused_and_counted(make: Any) -> None:
    model = Recorder([tools_reply(call("nope", x=1)), finish_reply()])
    b = make(model)
    assert b.drive().outcome == "complete"
    refused = b.completed(f"{T01}/c01").payload
    assert (refused["outcome"], refused["code"], refused["counted"]) == (
        "refused", "unknown_tool", True,
    )  # fmt: skip
    assert b.store.by_dedup_key(OWNER, f"sstart:{T01}/c01") is None
    assert meter(b.view(), RUN).used["tool_calls"] == 3  # the refusal, the save, the finish
    shown = [m.text or "" for m in model.requests[1].messages if m.role == "tool"]
    assert any("unknown_tool" in t for t in shown)


def test_a_spent_budget_ends_the_turn_and_raises_the_budget_interrupt(make: Any) -> None:
    budget = {"model_turns": 10, "tool_calls": 1, "active_ms": 10**9}
    model = Recorder(
        [tools_reply(call("run_tests", 1), call("run_tests", 2), call("run_tests", 3))]
    )
    b = make(model, budget=budget)
    result = b.drive()
    assert result.state == "held" and result.turns[-1].kind == "budget"
    assert b.bodies.calls == ["run_tests"]
    second, third = b.completed(f"{T01}/c02").payload, b.completed(f"{T01}/c03").payload
    assert (second["outcome"], second["code"]) == ("refused", "budget_exhausted")
    assert (third["outcome"], third["code"]) == ("skipped", "after_budget")
    held = b.decisions()[-1].payload
    assert (held["candidate"], held["verdict"]) == ("budget", "hold")
    assert [e.payload["kind"] for e in b.events("interrupt.raised")] == ["budget"]


def test_stop_request_honoured_at_next_turn_end(make: Any) -> None:
    model = Recorder([tools_reply(call("run_tests"), call("read_file", path="a"))])
    b = make(model)
    b.stop_after(f"{T01}/c01")
    result = b.drive()
    assert (result.state, result.outcome) == ("ended", "stopped")
    assert b.bodies.calls == ["run_tests"]
    assert b.completed(f"{T01}/c02").payload["code"] == "run_stopped"
    assert model.steps == [T01]


def test_a_turn_end_lost_before_its_decision_is_judged_once(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kill between a turn end and its stop decision: the restart judges that end once."""
    model = Recorder([tools_reply(call("run_tests")), finish_reply()])
    b = make(model)
    real = commit_stop_decision
    seen: list[str] = []

    def dies_on_the_second(store: Any, owner: str, d: Any) -> bool:
        seen.append(d.candidate.kind)
        if len(seen) == 2:
            raise Crash("killed before the turn end's decision commits")
        return real(store, owner, d)

    monkeypatch.setattr(drive_module, "commit_stop_decision", dies_on_the_second)
    with pytest.raises(Crash):
        b.drive()
    point = control_point(b.view(), RUN)
    assert point.candidate is not None and point.candidate.kind == "tools"
    monkeypatch.setattr(drive_module, "commit_stop_decision", real)
    b.restart()
    assert b.drive().outcome == "complete"
    assert model.steps == [T01, f"{RUN}/edit/t02"]
    assert [d.payload["candidate"] for d in b.decisions()] == ["next", "tools", "finish"]
    assert b.bodies.calls == ["run_tests"]


def test_a_recorded_turn_end_is_the_returned_one(make: Any) -> None:
    b = make(Recorder([tools_reply(call("run_tests"), SAVE_NOTE, FINISH)]))
    result = b.drive()
    assert recorded_turn_end(b.view(), EDIT, 1) == result.turns[0]


def _coder(limit: int, crash_at: int) -> Recorder:
    """The contrast coding agent's fake model: run the tests until turn ``limit``, then finish."""
    crashed: list[int] = []

    def rule(request: GenerationRequest) -> GenerationResult | BaseException:
        turn = StepId.parse(request.step_id).turn or 0
        if turn == crash_at and not crashed:
            crashed.append(turn)
            return Crash(f"killed during turn {turn}")
        if turn < limit:
            return tools_reply(call("run_tests", turn))
        return finish_reply()

    return Recorder(rule=rule)


def test_long_session_resume_spends_no_recorded_model_call(make: Any) -> None:
    """200 turns, a kill at 140, a restart: the live model calls after it number 61."""
    model = _coder(200, 140)
    b = make(model)
    with pytest.raises(Crash):
        b.drive()
    asked = len(model.requests)
    assert asked == 140
    b.restart()
    assert b.resume() == (RUN,)
    assert len(model.requests) - asked == 61
    assert b.ended()["outcome"] == "complete"
    assert len(b.bodies.calls) == 199


def test_summary_refusal_reads_the_budget_and_the_holds(make: Any) -> None:
    """ADR-P08 D7: a summary step is approved by the run's limits, judged on the log."""
    from aksha.runtime.cancellation.command import stop
    from aksha.runtime.loop.turn import stop_view
    from aksha.runtime.stop_policy.evaluation import SUMMARY_BUDGET, SUMMARY_HELD, summary_refusal

    model = Recorder([tools_reply(call("read_file", path="a.py")), finish_reply()])
    b = make(model, budget={"model_turns": 1, "tool_calls": 1_000, "active_ms": 10**9})
    assert summary_refusal(stop_view(b.setup, RUN), EDIT) is None
    b.drive()  # one turn spends the only model turn; the budget interrupt holds the run
    assert summary_refusal(stop_view(b.setup, RUN), EDIT) == SUMMARY_HELD
    (reached,) = b.events("budget.reached")
    spent = b.view().upto(reached.seq - 1)  # spent, before the interrupt was raised
    assert summary_refusal(stop_view(b.setup, RUN, spent), EDIT) == SUMMARY_BUDGET
    other = make(Recorder([finish_reply()]))
    stop(other.store, OWNER, RUN)
    assert summary_refusal(stop_view(other.setup, RUN), EDIT) == SUMMARY_HELD
