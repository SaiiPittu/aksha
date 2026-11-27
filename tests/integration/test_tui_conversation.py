"""The chat's client against a real ``aksha serve``: the offline conversation, end to end.

The terminal UI starts the server itself (``connection.start_server``, fake mode, the
bundled synthetic stillroom world, the mock counsel mailbox bound), uploads the world's
sources, sends the request, and drives the conversation from its own SSE stream and
reducer: the input question, the choice, then the approval with the signature the
transcript shows, until the run ends complete. Textual is not needed: this is the
client and the fold the app uses, with no screen.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from aksha import data
from aksha.app import SERVER_FILE
from aksha.interfaces.tui.client import Api, Synced, read_inputs
from aksha.interfaces.tui.connection import start_server
from aksha.interfaces.tui.reducer import Approval, Outcome, Question, Transcript

REQUEST = "Assess the four invention candidates and prepare the chosen one for counsel."
GAP = "0.5 m from the vent edge, so its air flow stays outside the footprint."
SESSION = "S-tui-0001"
DEADLINE_S = 120.0


def test_the_offline_conversation_runs_to_complete_through_the_chat_client(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    env = {
        "AKSHA_HOME": str(home),
        "AKSHA_MODE": "fake",
        "AKSHA_SYSTEMS": f"counsel_mail=mailbox:{home / 'mailbox'}",
    }
    connection = start_server(env)
    stop = threading.Event()
    holder: list[Api] = []

    def give_up() -> None:
        stop.set()
        for api in holder:
            api.close_streams()

    timer = threading.Timer(DEADLINE_S, give_up)
    timer.start()
    try:
        assert (home / SERVER_FILE).exists() and connection.started_here
        api = Api(connection.url, "local")
        holder.append(api)
        sources = Path(data.__file__).parent / "worlds" / "stillroom" / "sources"
        assert api.upload(read_inputs([str(sources)])).accepted
        assert api.send_message(SESSION, REQUEST, "patent").accepted
        transcript = Transcript(SESSION)
        decided: dict[str, str] = {}
        stream = api.events(SESSION, None, stop)
        for item in stream:
            if isinstance(item, Synced):
                transcript.synchronized = True
                continue
            if not isinstance(item, dict):
                continue
            transcript.apply(item)
            for waiting in transcript.pending():
                if waiting.id in decided:
                    continue
                if isinstance(waiting, Question) and waiting.qkind == "input":
                    receipt = api.answer(waiting.id, text=GAP)
                    decided[waiting.id] = GAP
                elif isinstance(waiting, Question) and waiting.qkind == "choice":
                    assert waiting.options == ["C1", "C2", "C3", "C4"]
                    receipt = api.answer(waiting.id, text=waiting.options[0])
                    decided[waiting.id] = waiting.options[0]
                elif isinstance(waiting, Approval):
                    assert waiting.tool == "send_packet"
                    assert ("recipient", "counsel-primary") in waiting.facts
                    receipt = api.approve(waiting.id, waiting.signature)
                    decided[waiting.id] = waiting.signature
                else:
                    raise AssertionError(f"unexpected {waiting}")
                assert receipt.accepted, receipt
            if any(isinstance(i, Outcome) for i in transcript.items):
                break  # closes the stream's connection, so the server stops at once
        [outcome] = [i for i in transcript.items if isinstance(i, Outcome)]
        assert outcome.outcome == "complete"
        kinds = [type(transcript.by_id[i]).__name__ for i in decided]
        assert kinds == ["Question", "Question", "Approval"]
        [approval] = [i for i in transcript.items if isinstance(i, Approval)]
        assert approval.state == "granted" and decided[approval.id] == approval.signature
        assert stream.cursor is not None and stream.cursor > 0
    finally:
        timer.cancel()
        stop.set()
        connection.close()
    assert connection.process is not None and connection.process.returncode is not None
    assert not (home / SERVER_FILE).exists()
