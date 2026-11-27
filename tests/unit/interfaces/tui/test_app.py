"""The chat app under Textual's pilot: cards, lanes, Ctrl+C, the drawer and the picker.

The stream is the recorded conversation (synthetic; tests/support/tui.py) played by an
in-memory ChatApi that logs every command, so each test checks exactly what was sent.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

pytest.importorskip("textual")

from rich.console import Console
from textual.pilot import Pilot
from textual.widgets import Input, OptionList, Static

from aksha.interfaces.tui.app import ApprovalCard, ChatApp, Composer, QuestionCard
from aksha.interfaces.tui.screens import SessionPicker
from tests.support.tui import SESSION, FakeApi, recorded, until

SIGNATURE = "v1:d5ffe1ab8cb85a1f2e56bc962acf1e72243ef7ee65f391386de1cffc28358baf"
SIZE = (130, 42)

Body = Callable[[ChatApp, Pilot[int], FakeApi], Coroutine[Any, Any, None]]


def run(api: FakeApi, body: Body, **kwargs: Any) -> None:
    async def main() -> None:
        app = ChatApp(api, SESSION, **kwargs)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, lambda: app.transcript.synchronized)
            await body(app, pilot, api)

    asyncio.run(main())


async def settle(pilot: Pilot[int], ready: Callable[[], bool], timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if ready():
            await pilot.pause()
            return
        await pilot.pause(0.02)
    raise AssertionError("timed out")


def text_of(widget: Static) -> str:
    """What a Static shows, as plain text (Rich renderables included)."""
    console = Console(width=200, file=io.StringIO(), record=True)
    console.print(widget.content)
    return console.export_text()


def test_answering_a_question_sends_the_typed_answer() -> None:
    api = FakeApi(until(recorded("conversation.jsonl"), "interrupt.raised", "I-0001"))

    async def body(app: ChatApp, pilot: Pilot[int], api: FakeApi) -> None:
        card = app.query_one(QuestionCard)
        await settle(pilot, lambda: card.has_class("shown") and bool(card.query(".q-input")))
        assert not app.query_one("#composer-row").display
        assert "Minimum distance from a vent" in text_of(app.query_one("#q-body", Static))
        await pilot.press(*"0.5 m", "enter")
        await settle(pilot, lambda: "answer" in api.names())
        assert api.calls == [
            ("answer", {"interrupt": "I-0001", "text": "0.5 m", "answers": None, "agent": None})
        ]

    run(api, body)


def test_a_number_picks_a_choice_option() -> None:
    api = FakeApi(until(recorded("conversation.jsonl"), "interrupt.raised", "I-0002"))

    async def body(app: ChatApp, pilot: Pilot[int], api: FakeApi) -> None:
        card = app.query_one(QuestionCard)
        await settle(
            pilot,
            lambda: (
                card.question is not None
                and card.question.id == "I-0002"
                and bool(card.query(".q-input"))
            ),
        )
        assert "1  C1" in text_of(app.query_one("#q-body", Static))
        await pilot.press("1", "enter")
        await settle(pilot, lambda: "answer" in api.names())
        assert api.calls[-1][1]["interrupt"] == "I-0002"
        assert api.calls[-1][1]["text"] == "C1"

    run(api, body)


def test_approving_shows_the_exact_facts_and_sends_the_full_signature() -> None:
    api = FakeApi(until(recorded("conversation.jsonl"), "approval.requested"))

    async def body(app: ChatApp, pilot: Pilot[int], api: FakeApi) -> None:
        card = app.query_one(ApprovalCard)
        await settle(pilot, lambda: card.has_class("shown") and app.focused is card)
        shown = text_of(app.query_one("#a-body", Static))
        for fact in (
            "send_packet",
            "Packet",
            "patent.packet/C1@v1",
            "Recipient",
            "counsel-primary",
            SIGNATURE,
        ):
            assert fact in shown
        assert api.calls == []  # nothing is decided by showing it
        await pilot.press("1")
        await settle(pilot, lambda: "approve" in api.names())
        assert api.calls == [("approve", {"approval": "A-0001", "signature": SIGNATURE})]
        await pilot.press("1")  # a second press sends nothing more
        await pilot.pause(0.1)
        assert api.names().count("approve") == 1

    run(api, body)


def test_escape_never_approves() -> None:
    api = FakeApi(until(recorded("conversation.jsonl"), "approval.requested"))

    async def body(app: ChatApp, pilot: Pilot[int], api: FakeApi) -> None:
        card = app.query_one(ApprovalCard)
        await settle(pilot, lambda: card.has_class("shown") and app.focused is card)
        await pilot.press("escape")
        await pilot.pause()
        assert not card.has_class("shown")
        assert app.query_one("#composer-row").display
        assert isinstance(app.focused, Composer)
        await pilot.press("escape", "1", "enter")  # Esc again, then "1" goes to the composer
        await settle(pilot, lambda: bool(api.calls))
        assert "approve" not in api.names() and "reject" not in api.names()
        assert api.calls[-1][0] == "steer" and api.calls[-1][1]["text"] == "1"
        await pilot.press("ctrl+r")
        await settle(pilot, lambda: card.has_class("shown"))
        assert "approve" not in api.names()

    run(api, body)


def test_rejecting_sends_the_note_and_escape_in_the_note_does_not_decide() -> None:
    api = FakeApi(until(recorded("conversation.jsonl"), "approval.requested"))

    async def body(app: ChatApp, pilot: Pilot[int], api: FakeApi) -> None:
        card = app.query_one(ApprovalCard)
        await settle(pilot, lambda: card.has_class("shown") and app.focused is card)
        await pilot.press("2")
        note = app.query_one("#a-note", Input)
        assert note.display and note.has_focus
        await pilot.press("escape")
        await pilot.pause()
        assert (note.display, card.has_class("shown"), api.calls) == (False, True, [])
        await pilot.press("2", *"wrong", "enter")
        await settle(pilot, lambda: bool(api.calls))
        assert api.calls == [("reject", {"approval": "A-0001", "note": "wrong"})]

    run(api, body)


def test_tab_cycles_the_lane_while_a_run_is_open() -> None:
    api = FakeApi(until(recorded("conversation.jsonl"), "run.started"))

    async def body(app: ChatApp, pilot: Pilot[int], api: FakeApi) -> None:
        lane = app.query_one("#lane", Static)
        assert "steer" in text_of(lane)
        await pilot.press("tab")
        assert "follow-up" in text_of(lane)
        await pilot.press("tab")
        assert "amend" in text_of(lane)
        await pilot.press("tab")
        assert "steer" in text_of(lane)
        await pilot.press("tab", *"Keep it short", "enter")
        await settle(pilot, lambda: bool(api.calls))
        assert api.calls == [
            ("steer", {"run": "R-0001", "text": "Keep it short", "lane": "follow_up"})
        ]

    run(api, body)


def test_with_no_open_run_a_message_starts_one_in_the_same_session() -> None:
    api = FakeApi(recorded("conversation.jsonl"))  # the whole run, ended complete

    async def body(app: ChatApp, pilot: Pilot[int], api: FakeApi) -> None:
        await settle(pilot, lambda: app.transcript.status() == "done")
        assert "message" in text_of(app.query_one("#lane", Static))
        await pilot.press("tab")  # no lane to cycle
        await pilot.press(*"And now C2", "enter")
        await settle(pilot, lambda: bool(api.calls))
        assert api.calls == [
            ("send_message", {"session": SESSION, "text": "And now C2", "agent": "patent"})
        ]

    run(api, body, agent="patent")


def test_ctrl_c_once_asks_to_stop_and_twice_quits() -> None:
    api = FakeApi(until(recorded("conversation.jsonl"), "run.started"))

    async def body(app: ChatApp, pilot: Pilot[int], api: FakeApi) -> None:
        await pilot.press("ctrl+c")
        assert "Stop R-0001?" in text_of(app.query_one("#status-line", Static))
        assert api.calls == []
        await pilot.press("y")
        await settle(pilot, lambda: bool(api.calls))
        assert api.calls[0][0] == "stop" and api.calls[0][1]["run"] == "R-0001"
        assert app.query_one(Composer).value == ""  # "y" confirmed, it was not typed
        await pilot.press("ctrl+c", "n")
        assert len(api.calls) == 1
        await pilot.press("ctrl+c", "ctrl+c")
        await pilot.pause()
        assert app.return_value == 0

    run(api, body)


def test_an_at_path_uploads_before_the_message(tmp_path: Any) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("# A\n")
    api = FakeApi([{"kind": "synchronized", "seq": 0}])

    async def body(app: ChatApp, pilot: Pilot[int], api: FakeApi) -> None:
        await pilot.press(*"Read @notes please", "enter")
        await settle(pilot, lambda: len(api.calls) == 2)
        assert api.calls[0] == ("upload", {"files": {"a.md": b"# A\n"}})
        assert api.calls[1][1]["text"] == "Read please"

    run(api, body, cwd=tmp_path)


def test_the_drawer_opens_versions_and_a_diff() -> None:
    api = FakeApi(
        recorded("conversation.jsonl"),
        artifact_content={
            "patent.packet/C1@v1": {"title": "Calibration", "claims": 3},
            "patent.packet/C1@v2": {"title": "Self-calibration", "claims": 3},
        },
    )

    async def body(app: ChatApp, pilot: Pilot[int], api: FakeApi) -> None:
        await pilot.press("ctrl+right_square_bracket")
        options = app.query_one("#drawer-list", OptionList)
        await settle(pilot, lambda: options.option_count == 23)  # one artifact, 22 documents
        assert "patent.packet/C1  v2" in str(options.get_option_at_index(0).prompt)
        await pilot.press("enter")
        view = app.query_one("#drawer-view", Static)
        await settle(pilot, lambda: "Self-calibration" in text_of(view))
        await pilot.press("left_square_bracket")
        await settle(pilot, lambda: '"Calibration"' in text_of(view))
        await pilot.press("right_square_bracket", "d")
        await settle(pilot, lambda: "v1 → v2" in text_of(view))
        assert '+  "title": "Self-calibration"' in text_of(view)

    run(api, body)


def test_the_session_picker_filters_to_waiting_and_resumes() -> None:
    rows: list[dict[str, Any]] = [
        {"session": "S-a", "title": "Done one", "status": "done", "agent": "patent", "run": "R-1"},
        {
            "session": "S-b",
            "title": "Needs you",
            "status": "waiting",
            "agent": "research",
            "run": "R-2",
            "waiting": {"kind": "approval", "id": "A-1"},
        },
    ]
    api = FakeApi([{"kind": "synchronized", "seq": 0}], session_rows=rows)

    async def body(app: ChatApp, pilot: Pilot[int], api: FakeApi) -> None:
        await pilot.press("ctrl+s")
        await settle(pilot, lambda: isinstance(app.screen, SessionPicker))
        options = app.screen.query_one(OptionList)
        assert options.option_count == 2
        await pilot.press("w")
        assert options.option_count == 1
        assert "◆ approval" in str(options.get_option_at_index(0).prompt)
        await pilot.press("enter")
        await settle(pilot, lambda: app.session == "S-b")
        assert not isinstance(app.screen, SessionPicker)

    run(api, body)
