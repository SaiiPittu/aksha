"""The transcript reducer over the recorded streams (synthetic; tests/support/tui.py)."""

from __future__ import annotations

from typing import Any

from aksha.interfaces.tui.reducer import (
    AgentTurn,
    Approval,
    Notice,
    Outcome,
    Question,
    Refusal,
    Transcript,
    UserMessage,
    fold,
    pairs,
    target_of,
)
from tests.support.tui import SESSION, events, until


def items_of(t: Transcript, kind: type) -> list[Any]:
    return [i for i in t.items if isinstance(i, kind)]


def test_the_conversation_folds_to_its_transcript() -> None:
    t = fold(SESSION, events("conversation.jsonl"))
    [user] = items_of(t, UserMessage)
    assert user.text.startswith("Assess the four invention candidates")
    assert user.agent == "patent"
    gap, choice = items_of(t, Question)
    assert (gap.id, gap.qkind, gap.state) == ("I-0001", "input", "resolved")
    assert gap.answers == {
        "q1": "0.5 m from the vent edge, so its air flow stays outside the footprint."
    }
    assert (choice.id, choice.qkind, choice.options) == (
        "I-0002",
        "choice",
        ["C1", "C2", "C3", "C4"],
    )
    assert choice.answers == {"q1": "C1"}
    [approval] = items_of(t, Approval)
    assert approval.state == "granted"
    [outcome] = items_of(t, Outcome)
    assert (outcome.run, outcome.outcome, outcome.agent) == ("R-0001", "complete", "patent")
    assert t.status() == "done"
    assert t.open_run is None
    assert t.title == user.text[:80]


def test_tool_calls_become_lines_with_target_and_duration() -> None:
    t = fold(SESSION, events("conversation.jsonl"))
    lines = [ln for turn in items_of(t, AgentTurn) for ln in turn.lines]
    calls = [ln for ln in lines if ln.status != "saved"]
    assert len(calls) == 160
    assert all(ln.status in ("ok", "error") for ln in calls)  # every started call completed
    read = next(ln for ln in calls if ln.name == "read_document")
    assert read.target.startswith("input/") and read.duration_ms is not None


def test_refusals_carry_their_code_and_reason() -> None:
    t = fold(SESSION, events("conversation.jsonl"))
    refused = [ln for turn in items_of(t, AgentTurn) for ln in turn.lines if ln.status == "error"]
    assert len(refused) == 4
    assert {(ln.name, ln.code, ln.reason) for ln in refused} == {
        ("web_search", "web_off", "no web search is configured")
    }
    [refusal] = items_of(t, Refusal)
    assert (refusal.command, refusal.code) == ("send_message", "lane_required")


def test_saved_artifacts_count_versions_per_slot() -> None:
    t = fold(SESSION, events("conversation.jsonl"))
    assert t.artifacts[("patent.packet", "C1")].versions == 1
    assert t.artifacts[("patent.packet", "C1")].ref == "patent.packet/C1@v1"
    saved = [
        ln.target for turn in items_of(t, AgentTurn) for ln in turn.lines if ln.status == "saved"
    ]
    assert "patent.packet/C1@v1" in saved
    assert len(saved) == sum(slot.versions for slot in t.artifacts.values())


def test_the_approval_holds_exactly_what_was_asked() -> None:
    rows = until(events("conversation.jsonl"), "approval.requested")
    t = fold(SESSION, rows)
    [approval] = t.pending()
    assert isinstance(approval, Approval)
    assert approval.tool == "send_packet"
    assert approval.facts == [("packet", "patent.packet/C1@v1"), ("recipient", "counsel-primary")]
    assert approval.targets == ["patent.packet/C1@v1"]
    assert (
        approval.signature == "v1:d5ffe1ab8cb85a1f2e56bc962acf1e72243ef7ee65f391386de1cffc28358baf"
    )
    assert (
        approval.canonical_args == '{"packet":"patent.packet/C1@v1","recipient":"counsel-primary"}'
    )
    assert t.status() == "waiting"
    assert t.open_run is not None and t.open_run.run == "R-0001"


def test_open_questions_are_pending_until_resolved() -> None:
    rows = until(events("conversation.jsonl"), "interrupt.raised", "I-0001")
    t = fold(SESSION, rows)
    [q] = t.pending()
    assert isinstance(q, Question) and q.id == "I-0001" and q.state == "open"
    assert q.questions == [
        ("q1", "C2: the note leaves Minimum distance from a vent undecided. What is it?")
    ]


def test_a_resumed_stream_ends_equal_to_the_full_replay() -> None:
    full = events("conversation.jsonl")
    head = [e for e in full if e["seq"] <= 449]
    resumed = fold(SESSION, head)
    for event in events("conversation.after-449.jsonl"):
        resumed.apply(event)
    whole = fold(SESSION, full)
    assert resumed.items == whole.items
    assert resumed.cursor == whole.cursor


def test_an_event_at_or_below_the_cursor_is_ignored() -> None:
    full = events("conversation.jsonl")
    t = fold(SESSION, full)
    before = list(t.items)
    for event in full[:50]:
        assert t.apply(event) == []
    assert t.items == before


def test_uploads_fold_into_one_notice() -> None:
    t = fold(SESSION, events("conversation.jsonl"))
    upload = t.items[0]
    assert isinstance(upload, Notice) and upload.text.startswith("uploaded 22 files")
    assert len(t.documents) == 22


def test_follow_up_input_is_queued_then_read() -> None:
    t = fold(SESSION, events("conversation.jsonl"))
    [follow] = [n for n in items_of(t, Notice) if n.text.startswith("follow-up")]
    assert follow.text == "follow-up: Keep the packet short. · read by the run"


def test_the_send_is_shown_leaving_and_then_sent() -> None:
    t = fold(SESSION, events("conversation.jsonl"))
    [send] = [n for n in items_of(t, Notice) if "counsel_mail" in n.text]
    assert send.text.endswith("sent: message_id m-1")


def test_a_routed_run_stopped_with_its_question_voided() -> None:
    t = fold("S-ui-0002", events("route-and-stop.jsonl"))
    route = next(n for n in items_of(t, Notice) if n.glyph == "→")
    assert route.text.startswith("routed to research by the classifier (95%)")
    [q] = items_of(t, Question)
    assert (q.id, q.state, q.reason) == ("I-0004", "voided", "run_stopped")
    assert any(n.text.startswith("stop requested for R-0004") for n in items_of(t, Notice))
    [outcome] = items_of(t, Outcome)
    assert outcome.outcome == "stopped"
    assert t.status() == "stopped"
    steer = next(n for n in items_of(t, Notice) if n.text.startswith("steer:"))
    assert "Only EU vendors." in steer.text


def test_model_text_numbers_its_excerpts_as_citations() -> None:
    t = Transcript(SESSION)
    t.apply(
        {
            "seq": 1,
            "type": "run.started",
            "run": "R-1",
            "session": SESSION,
            "payload": {"agent": "research", "cause": "request"},
        }
    )
    t.apply(
        {
            "seq": 2,
            "type": "excerpts.minted",
            "run": "R-1",
            "payload": {
                "spans": [
                    {"span_id": "EX-0007", "record_id": "web/a"},
                    {"span_id": "EX-0009", "record_id": "input/b.md"},
                ]
            },
        }
    )
    text = "Radar is common [EX-0009], thermal less so EX-0007; radar again [EX-0009]."
    t.apply(
        {
            "seq": 3,
            "type": "step.completed",
            "run": "R-1",
            "step": "R-1/answer/t01",
            "payload": {"kind": "model", "value": {"result": {"text": text, "tool_calls": []}}},
        }
    )
    [turn] = items_of(t, AgentTurn)
    assert turn.text == ["Radar is common [1], thermal less so [2]; radar again [1]."]
    assert [(c.n, c.span, c.record) for c in turn.citations] == [
        (1, "EX-0009", "input/b.md"),
        (2, "EX-0007", "web/a"),
    ]
    assert t.citation(2) is not None


def test_a_rebuild_run_is_announced() -> None:
    t = Transcript(SESSION)
    t.apply({"seq": 1, "type": "source.changed", "payload": {"records": {"input/a": "h"}}})
    t.apply(
        {
            "seq": 2,
            "type": "run.started",
            "run": "R-0002",
            "session": None,
            "payload": {"agent": "patent", "cause": "rebuild", "for_runs": ["R-0001"]},
        }
    )
    changed, rebuild = items_of(t, Notice)
    assert changed.text.startswith("inputs changed: 1 record moved")
    assert rebuild.text.startswith("rebuilding R-0002 (patent) for R-0001")
    assert t.open_run is None  # a rebuild run takes no steering from the composer


def test_facts_keep_their_order_from_either_shape() -> None:
    assert pairs({"b": "2", "a": 1}) == [("b", "2"), ("a", "1")]
    assert pairs([["recipient", "x"], ["packet", "y"]]) == [("recipient", "x"), ("packet", "y")]


def test_a_tool_lines_target_prefers_named_arguments() -> None:
    assert target_of({"limit": 3, "path": "input/a.md"}) == "input/a.md"
    assert target_of({"q": "occupancy sensors"}) == "occupancy sensors"
    assert target_of(None) == ""
    assert len(target_of({"text": "x" * 500})) == 56
