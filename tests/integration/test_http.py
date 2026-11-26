"""ADR-P19 over HTTP: commands with receipts, refusals as errors, reads, and the SSE stream."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aksha.app import open_reader, open_session, session_capabilities
from aksha.interfaces.http.server import create_app
from aksha.runtime.session.worker import RuntimeThread
from tests.support.session import OWNER, SESSION, config

H = {"X-Aksha-User": OWNER}


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[TestClient, Any]]:
    cfg = config(tmp_path)
    runtime = RuntimeThread(lambda: open_session(cfg))
    runtime.start()

    def record() -> dict[str, Any]:
        return runtime.call(lambda s: session_capabilities(s, cfg).as_json()).result(10)

    app = create_app(runtime, lambda: open_reader(cfg), record, heartbeat_s=0.05)
    with TestClient(app) as client:
        yield client, app
    app.state.closing.set()
    runtime.stop()


def _wait_run(client: TestClient, predicate: Any, timeout: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/v1/sessions/{SESSION}", headers=H).json()
        if data["run"] is not None and predicate(data["run"]):
            return dict(data["run"])
        time.sleep(0.05)
    raise AssertionError("the run never reached the expected state")


def test_a_message_returns_a_receipt_and_the_run_moves_on_the_writer(
    served: tuple[TestClient, Any],
) -> None:
    client, _ = served
    r = client.post(f"/api/v1/sessions/{SESSION}/messages", headers=H,
                    json={"text": "Write a note", "command_id": "c-1"})  # fmt: skip
    assert r.status_code == 200, r.text
    receipt = r.json()["receipt"]
    assert receipt["status"] == "accepted" and receipt["replayed"] is False
    again = client.post(f"/api/v1/sessions/{SESSION}/messages", headers=H,
                        json={"text": "Write a note", "command_id": "c-1"})  # fmt: skip
    assert again.json()["receipt"]["replayed"] is True
    run = _wait_run(client, lambda r: r["state"] == "ended")
    assert run["outcome"] == "complete"
    report = client.get(f"/api/v1/runs/{run['run']}", headers=H).json()
    assert report["outcome"] == "complete" and report["steps"]


def test_refusals_map_to_error_statuses(served: tuple[TestClient, Any]) -> None:
    client, _ = served
    client.post(f"/api/v1/sessions/{SESSION}/messages", headers=H,
                json={"text": "Write a note", "command_id": "c-1"})  # fmt: skip
    busy = client.post(f"/api/v1/sessions/{SESSION}/messages", headers=H,
                       json={"text": "And another", "command_id": "c-2"})  # fmt: skip
    if busy.status_code == 200:  # the first run already ended; still a receipt either way
        assert busy.json()["receipt"]["status"] == "accepted"
    else:
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "lane_required"
        assert busy.json()["error"]["details"]["status"] == "refused"
    reused = client.post(f"/api/v1/sessions/{SESSION}/messages", headers=H,
                         json={"text": "Different words", "command_id": "c-1"})  # fmt: skip
    assert reused.status_code == 409 and reused.json()["error"]["code"] == "command_id_reused"
    bad = client.post("/api/v1/commands", headers=H, json={"command": "Launch", "args": {}})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation"
    body = {"command": "Stop", "args": {"run_id": "R-1", "force": True}}
    extra = client.post("/api/v1/commands", headers=H, json=body)
    assert extra.status_code == 422
    missing = client.post("/api/v1/commands", headers=H,
                          json={"command": "Stop", "args": {"run_id": "R-9999"}})  # fmt: skip
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert client.get("/api/v1/runs/R-9999", headers=H).status_code == 404
    assert client.get("/api/v1/runs", headers={}).status_code == 422  # the owner is required


def test_another_owner_sees_nothing(served: tuple[TestClient, Any]) -> None:
    client, _ = served
    client.post(f"/api/v1/sessions/{SESSION}/messages", headers=H,
                json={"text": "Write a note", "command_id": "c-1"})  # fmt: skip
    _wait_run(client, lambda r: r["state"] == "ended")
    other = {"X-Aksha-User": "o2"}
    assert client.get("/api/v1/runs", headers=other).json() == {"runs": []}
    assert client.get("/api/v1/runs/R-0001", headers=other).status_code == 404


def test_answer_and_approve_through_their_endpoints(served: tuple[TestClient, Any]) -> None:
    client, _ = served
    client.post(f"/api/v1/sessions/{SESSION}/messages", headers=H,
                json={"text": "Ask first please", "command_id": "c-1"})  # fmt: skip
    run = _wait_run(client, lambda r: r["state"] == "waiting")
    (waiting,) = run["waiting_on"]
    r = client.post(f"/api/v1/interrupts/{waiting['interrupt_id']}/answer", headers=H,
                    json={"answers": {"q1": "blue"}})  # fmt: skip
    assert r.status_code == 200, r.text
    run = _wait_run(client, lambda r: r["state"] == "ended")
    assert run["outcome"] == "complete"


def test_the_event_stream_replays_marks_the_tail_and_follows(
    served: tuple[TestClient, Any],
) -> None:
    client, app = served
    client.post(f"/api/v1/sessions/{SESSION}/messages", headers=H,
                json={"text": "Write a note", "command_id": "c-1"})  # fmt: skip
    _wait_run(client, lambda r: r["state"] == "ended")
    timer = threading.Timer(0.5, app.state.closing.set)
    timer.start()
    body = client.get(f"/api/v1/sessions/{SESSION}/events", headers=H).text
    timer.join()
    blocks = [b for b in body.split("\n\n") if b.strip()]
    events = [b for b in blocks if b.startswith("id:") and "event: synchronized" not in b]
    marker = [b for b in blocks if "event: synchronized" in b]
    assert len(marker) == 1 and blocks.index(marker[0]) == len(events)
    seqs = [int(b.split("\n")[0].removeprefix("id: ")) for b in events]
    assert seqs == sorted(seqs) and len(seqs) > 5
    first = json.loads(events[0].split("data: ", 1)[1])
    assert first["type"] == "command.accepted" and first["kind"] == "event"
    assert ": heartbeat" in body
    app.state.closing.clear()
    timer = threading.Timer(0.5, app.state.closing.set)
    timer.start()
    resumed = client.get(f"/api/v1/sessions/{SESSION}/events",
                         headers={**H, "Last-Event-ID": str(seqs[-2])}).text  # fmt: skip
    timer.join()
    ids = [int(line.removeprefix("id: ")) for line in resumed.splitlines()
           if line.startswith("id: ")]  # fmt: skip
    assert ids[0] == seqs[-1]


def test_capabilities_health_and_verify(served: tuple[TestClient, Any]) -> None:
    client, _ = served
    caps = client.get("/api/v1/capabilities").json()
    assert "SendMessage" in caps["commands"] and "Steer" not in caps["commands"]
    assert client.get("/api/v1/health").json()["ok"] is True
    client.post(f"/api/v1/sessions/{SESSION}/messages", headers=H,
                json={"text": "Write a note", "command_id": "c-1"})  # fmt: skip
    _wait_run(client, lambda r: r["state"] == "ended")
    verified = client.get("/api/v1/verify").json()
    assert verified["ok"] is True and verified["seq"] > 0


def test_a_string_where_a_bool_belongs_is_refused_not_promoted(
    served: tuple[TestClient, Any],
) -> None:
    client, _ = served
    body = {"command": "SendMessage",
            "args": {"session": SESSION, "text": "Write a note", "headless": "false"}}  # fmt: skip
    r = client.post("/api/v1/commands", headers=H, json=body)
    assert r.status_code == 422 and "headless" in r.json()["error"]["message"]
    r = client.post(f"/api/v1/sessions/{SESSION}/messages", headers=H,
                    json={"text": "Write a note", "headless": 1})  # fmt: skip
    assert r.status_code == 422
