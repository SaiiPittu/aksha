"""The terminal UI's HTTP and SSE client: SSE parsing, the cursor and reconnects, commands."""

from __future__ import annotations

import io
import json
import threading
import urllib.error
from collections.abc import Iterator
from typing import Any

import pytest

from aksha.interfaces.tui.client import (
    Api,
    ApiError,
    EventStream,
    Synced,
    inputs_body,
    multipart,
    parse_sse,
    sessions_from_runs,
)
from tests.support.tui import FIXTURES, recorded, sse_text


def test_parse_sse_reads_the_recorded_stream() -> None:
    text = (FIXTURES / "route-and-stop.sse.txt").read_text(encoding="utf-8")
    blocks = list(parse_sse(text.splitlines(True)))
    comments = [b for b in blocks if b.comment is not None]
    assert [c.comment for c in comments] == ["heartbeat"]
    data = [b for b in blocks if b.comment is None]
    assert data[0].event == "document.stored" and data[0].id == "2"
    assert json.loads(data[0].data)["payload"]["doc_key"] == "D-0001"
    assert any(b.event == "synchronized" for b in data)


def test_parse_sse_joins_data_lines_and_ends_a_trailing_block() -> None:
    blocks = list(parse_sse(["id: 7\n", "event: x\n", 'data: {"a":\n', "data: 1}\n"]))
    assert blocks[0].id == "7" and json.loads(blocks[0].data) == {"a": 1}


def scripted(*connections: str | Exception) -> tuple[list[int | None], Any]:
    """A connect function that plays one scripted connection per call, recording the cursor."""
    asked: list[int | None] = []
    queue = list(connections)

    def connect(after: int | None) -> Iterator[str]:
        asked.append(after)
        if not queue:
            raise OSError("no more connections")
        part = queue.pop(0)
        if isinstance(part, Exception):
            raise part
        yield from part.splitlines(True)

    return asked, connect


def take(stream: EventStream, n: int) -> list[Any]:
    out = []
    for item in stream:
        out.append(item)
        if len(out) == n:
            stream.stop.set()
    return out


def test_a_reconnect_resumes_after_the_cursor_without_loss_or_repeat() -> None:
    rows = [r for r in recorded("route-and-stop.jsonl") if r.get("kind") == "event"]
    first, second = rows[:10], rows[10:]
    # the server drops the connection after ten events; the second connection replays from
    # the cursor, and also re-sends the tenth event, which must be dropped
    asked, connect = scripted(sse_text(first), sse_text(rows[9:]))
    waits: list[float] = []
    stream = EventStream(connect, sleep=waits.append)
    got = take(stream, len(rows))
    assert asked == [None, first[-1]["seq"]]
    assert [e["seq"] for e in got] == [r["seq"] for r in first + second]
    assert stream.reconnects == 1 and waits == [0.2]


def test_reconnects_back_off_and_stop_when_asked() -> None:
    stop = threading.Event()
    waits: list[float] = []

    def sleep(s: float) -> None:
        waits.append(s)
        if len(waits) == 7:
            stop.set()

    asked, connect = scripted(*[ConnectionRefusedError("down")] * 10)
    stream = EventStream(connect, stop=stop, sleep=sleep)
    assert list(stream) == []
    assert waits == [0.2, 0.5, 1.0, 2.0, 5.0, 5.0, 5.0]
    assert asked == [None] * 7
    assert len(stream.errors) == 7


def test_synchronized_marks_the_end_of_the_replay_and_heartbeats_are_skipped() -> None:
    rows = recorded("route-and-stop.jsonl")
    text = (FIXTURES / "route-and-stop.sse.txt").read_text(encoding="utf-8")
    _, connect = scripted(text)
    stream = EventStream(connect, sleep=lambda _: None)
    got = take(stream, len(rows))
    assert sum(1 for g in got if isinstance(g, Synced)) == 1
    assert stream.cursor == max(r["seq"] for r in rows if r.get("kind") == "event")


def test_the_cursor_survives_a_drop_after_synchronized() -> None:
    rows = [r for r in recorded("route-and-stop.jsonl") if r.get("kind") == "event"]
    sync = {"kind": "synchronized", "seq": rows[4]["seq"]}
    asked, connect = scripted(sse_text([*rows[:5], sync]), sse_text(rows[5:]))
    stream = EventStream(connect, sleep=lambda _: None)
    got = take(stream, len(rows) + 1)
    assert asked == [None, rows[4]["seq"]]
    assert stream.synchronized is False  # the second connection sent no marker
    assert [g["seq"] for g in got if not isinstance(g, Synced)] == [r["seq"] for r in rows]


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class Recorder:
    """An opener that records requests and answers from a script."""

    def __init__(self, *answers: bytes | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: float = 0) -> FakeResponse:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(answer)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.data) for r in self.requests if r.data and r.data[:1] == b"{"]


def receipt(cid: str = "c-1") -> bytes:
    return json.dumps(
        {
            "receipt": {
                "command_id": cid,
                "seq": 3,
                "status": "accepted",
                "code": None,
                "replayed": False,
            }
        }
    ).encode()


def http_error(status: int, body: dict[str, Any]) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://x",
        status,
        "err",
        {},  # type: ignore[arg-type]
        io.BytesIO(json.dumps(body).encode()),
    )


def test_each_command_gets_a_fresh_command_id() -> None:
    opener = Recorder(receipt(), receipt())
    api = Api("http://127.0.0.1:1", "local", opener=opener)
    api.send_message("S-1", "hello", "patent")
    api.send_message("S-1", "hello", "patent")
    first, second = opener.bodies()
    assert first["command_id"] != second["command_id"]
    assert first["command_id"].startswith("c-")
    assert first["agent"] == "patent" and first["text"] == "hello"


def test_a_dropped_connection_retries_with_the_same_command_id() -> None:
    opener = Recorder(urllib.error.URLError("reset"), receipt())
    api = Api("http://127.0.0.1:1", "local", opener=opener)
    got = api.approve("A-0001", "v1:" + "ab" * 32)
    first, retry = opener.bodies()
    assert first == retry
    assert first["shown_signature"] == "v1:" + "ab" * 32
    assert got.accepted


def test_a_refusal_is_a_refused_receipt() -> None:
    refused = {
        "command_id": "c-9",
        "seq": 4,
        "status": "refused",
        "code": "lane_required",
        "replayed": False,
    }
    opener = Recorder(
        http_error(409, {"error": {"code": "lane_required", "message": "open", "details": refused}})
    )
    api = Api("http://127.0.0.1:1", "local", opener=opener)
    got = api.send_message("S-1", "again", None)
    assert not got.accepted and got.refused == "lane_required"


def test_the_token_goes_as_a_bearer_and_the_owner_as_the_header() -> None:
    opener = Recorder(receipt())
    api = Api("http://127.0.0.1:1", "maya", token="aksha_pat_x", opener=opener)
    api.stop("R-0001", "enough")
    request = opener.requests[0]
    assert request.get_header("Authorization") == "Bearer aksha_pat_x"
    assert request.get_header("X-aksha-user") == "maya"
    assert request.full_url.endswith("/api/v1/runs/R-0001/stop")


def test_upload_falls_back_to_json_inputs_on_a_server_without_multipart() -> None:
    opener = Recorder(http_error(404, {"detail": "Not Found"}), receipt())
    api = Api("http://127.0.0.1:1", "local", opener=opener)
    api.upload({"notes/a.md": b"# A\n", "img.bin": b"\xff\x00"})
    multipart_request, json_request = opener.requests
    assert multipart_request.full_url.endswith("/inputs/upload")
    assert json_request.full_url.endswith("/inputs")
    body = json.loads(json_request.data)
    assert body["files"] == {"notes/a.md": "# A\n"} and body["binary"] == {"img.bin": "/wA="}


def test_a_missing_read_is_none_but_a_real_not_found_raises() -> None:
    opener = Recorder(
        http_error(404, {"detail": "Not Found"}),
        http_error(404, {"error": {"code": "not_found", "message": "no such"}}),
    )
    api = Api("http://127.0.0.1:1", "local", opener=opener)
    assert api.artifact("patent.packet/C1@v1") is None
    with pytest.raises(ApiError) as e:
        api.artifact("patent.packet/C9@v1")
    assert e.value.code == "not_found"


def test_sessions_fall_back_to_the_runs_of_each_session() -> None:
    rows = sessions_from_runs(
        [
            {
                "run": "R-1",
                "agent": "patent",
                "session": "S-a",
                "state": "ended",
                "outcome": "complete",
            },
            {
                "run": "R-2",
                "agent": "research",
                "session": "S-b",
                "state": "waiting",
                "waiting_on": [{"kind": "approval", "approval_id": "A-1", "interrupt_id": "A-1"}],
            },
            {
                "run": "R-3",
                "agent": "patent",
                "session": None,
                "state": "ended",
                "outcome": "complete",
            },
        ]
    )
    assert [(r["session"], r["status"]) for r in rows] == [("S-b", "waiting"), ("S-a", "done")]
    assert rows[0]["waiting"] == {"kind": "approval", "id": "A-1"}


def test_multipart_names_each_part_by_its_path() -> None:
    body, boundary = multipart({"a/b.txt": b"hi"}, {"command_id": "c-1"})
    assert f"--{boundary}--".encode() in body
    assert b'filename="a/b.txt"' in body and b'name="command_id"' in body
    assert inputs_body({"x": b"y"}) == {"files": {"x": "y"}}
