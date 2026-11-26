"""ADR-P27: a person's message to an open run is a durable fact in one of three lanes.

The scribe fixture writes the last ``note text: ...`` line a person's message holds, so
each test reads which message reached the model from the note it saved.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.session import named as n
from aksha.runtime.session.capabilities import capabilities
from tests.support.session import OWNER, SESSION, SessionBench

SENT: dict[str, list[str]] = {}
"""The user messages of every model request, by step ID (filled by ``bench``)."""


@pytest.fixture(autouse=True)
def record_requests(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import aksha.runtime.loop.turn as turn

    real = getattr(turn, "generation_step")  # noqa: B009 (the loop's own import)

    def spy(executor: Any, step: Any, table: Any, request: Any, **kw: Any) -> Any:
        SENT[str(step)] = [m.text or "" for m in request.messages if m.role == "user"]
        return real(executor, step, table, request, **kw)

    SENT.clear()
    monkeypatch.setattr(turn, "generation_step", spy)
    yield


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[SessionBench]:
    b = SessionBench(tmp_path)
    yield b
    b.close()


def send(b: SessionBench, text: str, cid: str = "c-1") -> n.SendMessage:
    return n.SendMessage(owner=OWNER, session=SESSION, text=text, command_id=cid)


def steer(text: str, cid: str, lane: str = "steer", **kw: Any) -> n.Steer:
    return n.Steer(owner=OWNER, run_id="R-0001", text=text, lane=lane,  # type: ignore[arg-type]
                   command_id=cid, **kw)  # fmt: skip


def saved_texts(b: SessionBench) -> list[str]:
    from aksha.runtime.artifacts.versions import all_versions
    from aksha.runtime.projections.view import load_view

    view = load_view(b.session.store, OWNER)
    versions = sorted(all_versions(view, blobs=b.session.blobs), key=lambda v: v.event_seq)
    return [str(v.content["text"]) for v in versions if v.ref.kind.endswith("note")]


def approve_waiting(b: SessionBench, cid: str) -> None:
    (waiting,) = b.session.status(OWNER, SESSION).waiting_on
    assert waiting.kind == "approval" and waiting.approval_id and waiting.signature
    ok = n.Approve(owner=OWNER, approval_id=waiting.approval_id,
                   shown_signature=waiting.signature, command_id=cid)  # fmt: skip
    assert b.session.dispatch(ok).accepted


def outcome(b: SessionBench) -> str | None:
    run = b.session.status(OWNER, SESSION).run
    return None if run is None else run.outcome


def user_texts(b: SessionBench, step: str) -> list[str]:
    """The user messages the model was sent at ``step`` (recorded by the spy)."""
    return SENT[step]


# --- D1 and the session API -------------------------------------------------------------


def test_steer_is_listed_and_a_bare_message_to_an_open_run_needs_a_lane(
    bench: SessionBench,
) -> None:
    assert "Steer" in bench.session.commands()
    assert "Steer" in capabilities(bench.session).commands
    bench.session.dispatch(send(bench, "Write a note and publish it"))
    bench.session.run_until_yield(OWNER)
    again = bench.session.dispatch(send(bench, "note text: blue", "c-2"))
    assert again.refused == "lane_required"


# --- D2 and D3: steer --------------------------------------------------------------------


def test_a_steer_is_queued_and_rendered_before_the_next_model_call(bench: SessionBench) -> None:
    bench.session.dispatch(send(bench, "Write a note and publish it"))
    bench.session.run_until_yield(OWNER)
    receipt = bench.session.dispatch(steer("note text: blue", "c-2"))
    assert receipt.accepted and not receipt.duplicate
    (queued,) = bench.payloads("input.queued")
    assert (queued["lane"], queued["text"], queued["input_id"]) == (
        "steer", "note text: blue", "IN-0001",
    )  # fmt: skip
    assert bench.events("input.consumed") == []
    approve_waiting(bench, "c-3")
    bench.session.run_until_yield(OWNER)
    assert outcome(bench) == "complete"
    assert saved_texts(bench) == ["blue"]
    (consumed,) = bench.events("input.consumed")
    assert consumed.payload["input_id"] == "IN-0001" and consumed.payload["by"] == "context"
    step = str(consumed.payload["step_id"])
    assert any("note text: blue" in t for t in user_texts(bench, step))


def test_a_retried_steer_is_one_input(bench: SessionBench) -> None:
    bench.session.dispatch(send(bench, "Write a note and publish it"))
    bench.session.run_until_yield(OWNER)
    first = bench.session.dispatch(steer("note text: blue", "c-2"))
    again = bench.session.dispatch(steer("note text: blue", "c-2"))
    assert again.duplicate and again.seq == first.seq
    assert len(bench.payloads("input.queued")) == 1


def test_a_steer_typed_before_a_restart_reaches_the_run_after_it(tmp_path: Path) -> None:
    first = SessionBench(tmp_path)
    try:
        first.session.dispatch(send(first, "Write a note and publish it"))
        first.session.run_until_yield(OWNER)
        assert first.session.dispatch(steer("note text: kept", "c-2")).accepted
    finally:
        first.close()
    second = SessionBench(tmp_path)
    try:
        second.session.recover()
        approve_waiting(second, "c-3")
        second.session.run_until_yield(OWNER)
        assert outcome(second) == "complete" and saved_texts(second) == ["kept"]
    finally:
        second.close()


def test_a_consumed_steer_stays_in_the_instances_history(bench: SessionBench) -> None:
    bench.session.dispatch(send(bench, "Write a note and publish it"))
    bench.session.run_until_yield(OWNER)
    bench.session.dispatch(steer("note text: blue", "c-2"))
    bench.session.dispatch(steer("note text: plain", "c-3", lane="follow_up"))
    approve_waiting(bench, "c-4")
    bench.session.run_until_yield(OWNER)
    first = str(bench.events("input.consumed")[0].payload["step_id"])
    last = list(SENT)[-1]
    assert first != last
    assert sum("note text: blue" in t for t in user_texts(bench, last)) == 1


# --- D4: follow-up -----------------------------------------------------------------------


def test_a_follow_up_vetoes_the_finish_and_is_rendered_after_it(bench: SessionBench) -> None:
    bench.session.dispatch(send(bench, "Write a note and publish it"))
    bench.session.run_until_yield(OWNER)
    assert bench.session.dispatch(steer("note text: green", "c-2", lane="follow_up")).accepted
    approve_waiting(bench, "c-3")
    bench.session.run_until_yield(OWNER)
    assert outcome(bench) == "complete"
    assert saved_texts(bench) == ["noted", "green"]
    vetoes = [p for p in bench.payloads("stop.decided")
              if p["verdict"] == "continue" and p.get("candidate") == "finish"]  # fmt: skip
    (veto,) = vetoes
    assert "note text: green" in veto["reason"]
    (consumed,) = bench.events("input.consumed")
    assert consumed.payload["by"] == "stop_policy"
    assert consumed.payload["step_id"].endswith(f"/t{veto['turn']:02d}")


# --- D5: amend ---------------------------------------------------------------------------


def test_an_amendment_supersedes_the_answer_and_invalidates_what_read_it(
    bench: SessionBench,
) -> None:
    bench.session.dispatch(send(bench, "Please ask first, then write"))
    bench.session.run_until_yield(OWNER)
    (waiting,) = bench.session.status(OWNER, SESSION).waiting_on
    answer = n.AnswerInterrupt(owner=OWNER, interrupt_id=waiting.interrupt_id,
                               text="note text: red", command_id="c-2")  # fmt: skip
    assert bench.session.dispatch(answer).accepted
    # A follow-up keeps the run open after the note is saved, so the amendment lands after.
    assert bench.session.dispatch(steer("check it once more", "c-9", lane="follow_up")).accepted
    assert bench.session.step(OWNER)  # the answer's decision: one more turn
    assert bench.session.step(OWNER)  # the turn saves the note; the follow-up vetoes its finish
    assert saved_texts(bench) == ["red"] and outcome(bench) is None
    receipt = bench.session.dispatch(steer("note text: violet", "c-3", lane="amend"))
    assert receipt.accepted, receipt
    (amended,) = bench.payloads("interrupt.amended")
    assert amended["interrupt"] == waiting.interrupt_id
    assert amended["answers"] == {"q1": "note text: violet"}
    minted = [s for p in bench.payloads("excerpts.minted") for s in p["spans"]]
    assert minted[-1]["supersedes"] == minted[-2]["span_id"]
    from aksha.runtime.invalidation.view import invalidation_view
    from aksha.runtime.projections.view import load_view

    view = invalidation_view(load_view(bench.session.store, OWNER), blobs=bench.session.blobs)
    assert [str(v.ref) for v in view.invalidated_latest()] == ["scribe.note@v1"]
    bench.session.run_until_yield(OWNER)
    assert outcome(bench) == "complete"
    assert saved_texts(bench) == ["red", "violet"]


def test_an_amendment_needs_an_answered_question(bench: SessionBench) -> None:
    bench.session.dispatch(send(bench, "Write a note and publish it"))
    bench.session.run_until_yield(OWNER)
    refused = bench.session.dispatch(steer("note text: violet", "c-2", lane="amend"))
    assert refused.refused == "nothing_to_amend"
    assert bench.payloads("input.queued") == []


def test_an_answer_can_be_amended_by_its_interrupt(bench: SessionBench) -> None:
    """``AnswerInterrupt(amend=True)``: the same path, naming the interrupt."""
    bench.session.dispatch(send(bench, "Please ask first, then write and publish it"))
    bench.session.run_until_yield(OWNER)
    (waiting,) = bench.session.status(OWNER, SESSION).waiting_on
    first = n.AnswerInterrupt(owner=OWNER, interrupt_id=waiting.interrupt_id,
                              text="note text: red", command_id="c-2")  # fmt: skip
    assert bench.session.dispatch(first).accepted
    bench.session.run_until_yield(OWNER)
    fix = n.AnswerInterrupt(owner=OWNER, interrupt_id=waiting.interrupt_id,
                            answers={"q1": "note text: teal"}, amend=True,
                            command_id="c-3")  # fmt: skip
    assert bench.session.dispatch(fix).accepted
    approve_waiting(bench, "c-4")
    bench.session.run_until_yield(OWNER)
    assert outcome(bench) == "complete" and saved_texts(bench) == ["teal"]


# --- refusals -----------------------------------------------------------------------------


def test_steer_refusals(bench: SessionBench) -> None:
    assert bench.session.dispatch(steer("x", "c-0")).refused == "not_found"
    bench.session.dispatch(send(bench, "Write a note and publish it"))
    bench.session.run_until_yield(OWNER)
    other = n.Steer(owner="o2", run_id="R-0001", text="x", command_id="c-2")
    assert bench.session.dispatch(other).refused == "not_found"
    assert bench.session.dispatch(steer("", "c-3")).refused == "text_required"
    assert bench.session.dispatch(steer("x", "c-4", lane="later")).refused == "lane_unknown"
    assert bench.session.dispatch(steer("x", "c-5", instance="nope")).refused == (
        "instance_not_open"
    )
    assert bench.session.dispatch(steer("x", "c-6", instance="write")).accepted
    approve_waiting(bench, "c-7")
    bench.session.run_until_yield(OWNER)
    assert outcome(bench) == "complete"
    assert bench.session.dispatch(steer("x", "c-8")).refused == "run_ended"


def test_an_answer_of_an_ended_run_cannot_be_amended(bench: SessionBench) -> None:
    """B8 for both ways in: an amendment to an ended run would rebuild nothing."""
    bench.session.dispatch(send(bench, "Please ask first, then write"))
    bench.session.run_until_yield(OWNER)
    (waiting,) = bench.session.status(OWNER, SESSION).waiting_on
    first = n.AnswerInterrupt(owner=OWNER, interrupt_id=waiting.interrupt_id,
                              text="note text: red", command_id="c-2")  # fmt: skip
    assert bench.session.dispatch(first).accepted
    bench.session.run_until_yield(OWNER)
    assert outcome(bench) == "complete"
    fix = n.AnswerInterrupt(owner=OWNER, interrupt_id=waiting.interrupt_id,
                            text="note text: teal", amend=True, command_id="c-3")  # fmt: skip
    assert bench.session.dispatch(fix).refused == "run_ended"
    assert bench.session.dispatch(steer("note text: teal", "c-4", lane="amend")).refused == (
        "run_ended"
    )
    assert bench.payloads("interrupt.amended") == []
