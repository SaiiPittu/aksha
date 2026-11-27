"""ADR-P19: one dispatch over named commands with receipts, passes between turns, statuses."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.app import RuntimeConfig
from aksha.runtime.projections.view import load_view
from aksha.runtime.session import named as n
from aksha.runtime.session.capabilities import capabilities
from aksha.runtime.session.errors import CommandIdReused
from aksha.runtime.session.views import inspect_run
from aksha.runtime.storage.errors import StoreError
from tests.support.session import OWNER, SESSION, SessionBench


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[SessionBench]:
    b = SessionBench(tmp_path)
    yield b
    b.close()


def send(b: SessionBench, text: str, cid: str = "c-1", **kw: object) -> n.SendMessage:
    return n.SendMessage(owner=OWNER, session=SESSION, text=text, command_id=cid, **kw)  # type: ignore[arg-type]


def test_a_message_starts_a_run_and_the_receipt_means_committed_not_done(
    bench: SessionBench,
) -> None:
    receipt = bench.session.dispatch(send(bench, "Write a note"))
    assert receipt.accepted and not receipt.duplicate
    assert [e.type for e in bench.events()] == [
        "command.accepted", "user.message", "route.decided", "run.started",
    ]  # fmt: skip
    started = bench.payloads("run.started")[0]
    assert started["agent"] == "scribe" and started["run_mode"] == "fake"
    assert started["budget"]["model_turns"] == 10 and "headless" not in started
    status = bench.session.status(OWNER, SESSION)
    assert status.run is not None and status.run.state == "running" and not status.yielded
    assert bench.session.run_until_yield(OWNER) > 0
    status = bench.session.status(OWNER, SESSION)
    assert status.run is not None and (status.run.state, status.run.outcome) == (
        "ended",
        "complete",
    )


def test_a_retried_command_returns_the_first_receipt_and_writes_nothing(
    bench: SessionBench,
) -> None:
    first = bench.session.dispatch(send(bench, "Write a note"))
    before = len(bench.events())
    again = bench.session.dispatch(send(bench, "Write a note"))
    assert again.duplicate and (again.seq, again.accepted) == (first.seq, True)
    assert len(bench.events()) == before
    with pytest.raises(CommandIdReused):
        bench.session.dispatch(send(bench, "Write another note"))


def test_two_deliberate_identical_messages_are_two_commands(bench: SessionBench) -> None:
    bench.session.dispatch(send(bench, "Write a note", "c-1"))
    second = bench.session.dispatch(send(bench, "Write a note", "c-2"))
    assert not second.accepted and second.refused == "lane_required"


def test_an_answer_typed_during_a_run_commits_between_passes(bench: SessionBench) -> None:
    bench.session.dispatch(send(bench, "Please ask first, then write"))
    bench.session.run_until_yield(OWNER)
    status = bench.session.status(OWNER, SESSION)
    assert status.run is not None and status.run.state == "waiting"
    (waiting,) = status.waiting_on
    assert (waiting.kind, waiting.label) == ("input", "needs_detail")
    answer = n.AnswerInterrupt(owner=OWNER, interrupt_id=waiting.interrupt_id,
                               answers={"q1": "the blue one"}, command_id="c-2")  # fmt: skip
    receipt = bench.session.dispatch(answer)
    assert receipt.accepted, receipt
    bench.session.run_until_yield(OWNER)
    status = bench.session.status(OWNER, SESSION)
    assert status.run is not None and status.run.outcome == "complete"


def test_a_stop_between_passes_ends_the_run_stopped(bench: SessionBench) -> None:
    bench.session.dispatch(send(bench, "Write a note"))
    assert bench.session.step(OWNER)  # one pass: the first stop-policy decision
    run = bench.session.status(OWNER, SESSION).run
    assert run is not None
    stop = bench.session.dispatch(n.Stop(owner=OWNER, run_id=run.run, command_id="c-2"))
    assert stop.accepted
    bench.session.run_until_yield(OWNER)
    run = bench.session.status(OWNER, SESSION).run
    assert run is not None and run.outcome == "stopped"


def test_an_approved_publish_is_delivered_once(bench: SessionBench) -> None:
    bench.session.dispatch(send(bench, "Write a note and publish it"))
    bench.session.run_until_yield(OWNER)
    status = bench.session.status(OWNER, SESSION)
    (waiting,) = status.waiting_on
    assert waiting.kind == "approval" and waiting.signature is not None
    assert waiting.approval_id is not None
    wrong = n.Approve(owner=OWNER, approval_id=waiting.approval_id,
                      shown_signature="v1:" + "0" * 64, command_id="c-2")  # fmt: skip
    assert bench.session.dispatch(wrong).refused == "signature_mismatch"
    ok = n.Approve(owner=OWNER, approval_id=waiting.approval_id,
                   shown_signature=waiting.signature, command_id="c-3")  # fmt: skip
    assert bench.session.dispatch(ok).accepted
    bench.session.run_until_yield(OWNER)
    status = bench.session.status(OWNER, SESSION)
    assert status.run is not None and status.run.outcome == "complete"
    assert len(bench.board.rows) == 1


def test_an_accepted_review_rebuilds_with_the_agents_approval_tools(
    bench: SessionBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-P17 B1: AcceptReview hands accept_review the agent's approval tools, so the fresh
    request after the accept is a body the outbox's re-check rebuilds unchanged."""
    import aksha.runtime.session.service as service
    from aksha.runtime.invalidation.commands import accept_review as real

    seen: dict[str, object] = {}

    def spy(*args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "accept_review", spy)
    bench.session.dispatch(send(bench, "Write a note and publish it"))
    bench.session.run_until_yield(OWNER)
    (waiting,) = bench.session.status(OWNER, SESSION).waiting_on
    accept = n.AcceptReview(owner=OWNER, review_id=waiting.interrupt_id,
                            card_hash="sha256:x", command_id="c-2")  # fmt: skip
    bench.session.dispatch(accept)
    tools = seen.get("tools")
    assert isinstance(tools, dict) and sorted(tools) == ["publish"]


def test_a_headless_start_needs_a_sandbox(tmp_path: Path) -> None:
    bare = SessionBench(tmp_path / "bare")
    try:
        refused = bare.session.dispatch(send(bare, "Write a note", headless=True))
        assert refused.refused == "sandbox_required"
        assert bare.payloads("run.started") == []
    finally:
        bare.close()
    boxed = SessionBench(tmp_path / "boxed", sandbox="outer:os-jail")
    try:
        assert boxed.session.dispatch(send(boxed, "Write a note", headless=True)).accepted
        assert boxed.payloads("run.started")[0]["headless"] is True
    finally:
        boxed.close()


def test_headless_turns_an_ask_into_an_allow_and_never_waits(tmp_path: Path) -> None:
    b = SessionBench(tmp_path, sandbox="outer:os-jail")
    try:
        b.session.dispatch(send(b, "Write a note and publish it", headless=True))
        b.session.run_until_yield(OWNER)
        status = b.session.status(OWNER, SESSION)
        assert status.run is not None and status.run.outcome == "complete"
        assert b.events("approval.requested") == [] and len(b.board.rows) == 1
    finally:
        b.close()


def test_another_owners_interrupt_reads_as_unknown(bench: SessionBench) -> None:
    bench.session.dispatch(send(bench, "Please ask first"))
    bench.session.run_until_yield(OWNER)
    (waiting,) = bench.session.status(OWNER, SESSION).waiting_on
    other = n.AnswerInterrupt(owner="o2", interrupt_id=waiting.interrupt_id, text="x",
                              command_id="c-9")  # fmt: skip
    receipt = bench.session.dispatch(other)
    assert not receipt.accepted
    mine = n.AnswerInterrupt(owner=OWNER, interrupt_id="I-9999", text="x", command_id="c-8")
    assert bench.session.dispatch(mine).refused == receipt.refused


def test_an_unsupported_command_is_refused_with_a_stored_receipt(bench: SessionBench) -> None:
    change = n.ApplySourceChange(owner=OWNER, change_set="cs-1", command_id="c-5")
    receipt = bench.session.dispatch(change)
    assert receipt.refused == "unsupported"
    assert bench.payloads("command.refused")[-1]["code"] == "unsupported"
    assert bench.session.dispatch(change).duplicate


def test_every_listed_command_reaches_its_handler(bench: SessionBench) -> None:
    """D7's honesty rule: a command the record lists is never refused ``unsupported``."""
    record = capabilities(bench.session)
    samples: dict[str, n.NamedCommand] = {
        "SendMessage": send(bench, "Write a note", "k-1"),
        "AnswerInterrupt": n.AnswerInterrupt(owner=OWNER, interrupt_id="I-1", text="x"),
        "Approve": n.Approve(owner=OWNER, approval_id="A-1", shown_signature="v1:x"),
        "Reject": n.Reject(owner=OWNER, approval_id="A-1"),
        "EditAction": n.EditAction(owner=OWNER, approval_id="A-1", changes={"text": "y"}),
        "Handoff": n.Handoff(owner=OWNER, approval_id="A-1"),
        "AcceptReview": n.AcceptReview(owner=OWNER, review_id="I-1", card_hash="sha256:x"),
        "RejectReview": n.RejectReview(owner=OWNER, review_id="I-1", card_hash="sha256:x"),
        "GrantBudget": n.GrantBudget(owner=OWNER, interrupt_id="I-1", extra={"model_turns": 1}),
        "Stop": n.Stop(owner=OWNER, run_id="R-0001"),
        "Finalize": n.Finalize(owner=OWNER, interrupt_id="I-1"),
        "Steer": n.Steer(owner=OWNER, run_id="R-0001", text="faster"),
        "ProposeCorrection": n.ProposeCorrection(
            owner=OWNER, target="note.draft", value="x", evidence=("why",)
        ),
        "DecideCorrection": n.DecideCorrection(owner=OWNER, correction_id="K-0001", approve=True),
    }
    assert set(record.commands) <= set(samples)
    for name in record.commands:
        receipt = bench.session.dispatch(samples[name])
        assert receipt.refused != "unsupported", name
    assert "Steer" in record.commands and "ApplySourceChange" not in record.commands


def test_the_capability_record_describes_the_loaded_agent(bench: SessionBench) -> None:
    record = capabilities(bench.session).as_json()
    (agent,) = record["agents"]
    assert agent["name"] == "scribe" and agent["approval_tools"] == ["publish"]
    assert agent["interrupt_labels"] == [["input", "needs_detail"]]
    assert record["mode"] == "fake" and record["headless"] is False
    assert record["stream"]["synchronized_marker"] is True and record["router"] is False


def test_the_inspector_reads_a_run_without_booting(bench: SessionBench) -> None:
    bench.session.dispatch(send(bench, "Write a note"))
    bench.session.run_until_yield(OWNER)
    run = bench.session.status(OWNER, SESSION).run
    assert run is not None
    report = inspect_run(load_view(bench.session.store, OWNER), run.run)
    assert report is not None and report["outcome"] == "complete"
    assert [s["kind"] for s in report["steps"]][:1] == ["model"]
    assert inspect_run(load_view(bench.session.store, OWNER), "R-9999") is None


@pytest.mark.parametrize(("value", "recorded"), [(None, "fresh"), ("fresh", "fresh"),
                                                 ("resume", "resume")])  # fmt: skip
def test_a_request_run_records_the_configured_reentry_setting(
    tmp_path: Path, value: str | None, recorded: str
) -> None:
    """Decision 36: a session's request runs can opt into resume; fresh is the default."""
    b = SessionBench(tmp_path) if value is None else SessionBench(tmp_path, reentry=value)
    try:
        b.session.dispatch(send(b, "Write a note"))
        assert b.payloads("run.started")[0]["reentry"] == recorded
    finally:
        b.close()


def test_an_unknown_reentry_setting_is_refused(tmp_path: Path) -> None:
    assert RuntimeConfig.from_env({}).reentry_setting() == "fresh"
    assert RuntimeConfig.from_env({"AKSHA_REENTRY": "resume"}).reentry_setting() == "resume"
    with pytest.raises(StoreError) as caught:
        SessionBench(tmp_path, reentry="sometimes")
    assert caught.value.code == "reentry_invalid"
