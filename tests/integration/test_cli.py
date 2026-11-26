"""The ``aksha`` command line: embedded mode, server mode, headless, and the exit codes."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.interfaces import wire
from aksha.interfaces.cli.main import main
from aksha.runtime.canonical import thaw
from tests.support.events import START
from tests.support.session import env

REPO = Path(__file__).resolve().parents[2]


def aksha(tmp: Path, *argv: str, **extra: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(["--user", "o1", *argv], out=out, env=env(tmp, **extra))
    return code, out.getvalue()


def test_run_embedded_completes_and_exits_zero(tmp_path: Path) -> None:
    code, out = aksha(tmp_path, "run", "Write a note", "--agent", "scribe", "--session", "S-1")
    assert code == wire.EXIT_OK, out
    assert re.search(r"R-0001 scribe ended complete", out)
    code, out = aksha(tmp_path, "status", "R-0001")
    assert code == wire.EXIT_OK and "ended complete" in out
    code, out = aksha(tmp_path, "inspect", "R-0001")
    assert code == wire.EXIT_OK and json.loads(out)["outcome"] == "complete"
    code, out = aksha(tmp_path, "verify")
    assert code == wire.EXIT_OK and out.startswith("chain ok")


def test_a_question_exits_three_and_the_answer_finishes_the_run(tmp_path: Path) -> None:
    code, out = aksha(tmp_path, "run", "Ask first, then write", "--session", "S-1")
    assert code == wire.EXIT_WAITING_INPUT, out
    interrupt = re.search(r"aksha answer (I-\d+)", out)
    assert interrupt is not None
    code, out = aksha(tmp_path, "answer", interrupt.group(1), "the blue one")
    assert code == wire.EXIT_OK, out
    assert "ended complete" in out


def test_an_approval_exits_four_and_a_wrong_signature_is_refused(tmp_path: Path) -> None:
    code, out = aksha(tmp_path, "run", "Write and publish", "--session", "S-1")
    assert code == wire.EXIT_WAITING_APPROVAL, out
    found = re.search(r"aksha approve (A-\d+) --signature (v1:[0-9a-f]{64})", out)
    assert found is not None
    code, out = aksha(tmp_path, "approve", found.group(1), "--signature", "v1:" + "0" * 64)
    assert code == wire.EXIT_REFUSED and "signature_mismatch" in out
    code, out = aksha(tmp_path, "reject", found.group(1), "--note", "not today")
    assert code != wire.EXIT_REFUSED, out


def test_an_approved_publish_is_delivered_through_the_extensions_system(tmp_path: Path) -> None:
    code, out = aksha(tmp_path, "run", "Write and publish", "--session", "S-1")
    assert code == wire.EXIT_WAITING_APPROVAL, out
    found = re.search(r"aksha approve (A-\d+) --signature (v1:[0-9a-f]{64})", out)
    assert found is not None
    code, out = aksha(tmp_path, "approve", found.group(1), "--signature", found.group(2))
    assert code == wire.EXIT_OK, out
    assert "ended complete" in out
    rows = tmp_path / "var" / "systems" / "board.ledger" / "rows.json"
    assert len(json.loads(rows.read_text(encoding="utf-8"))) == 1
    code, out = aksha(tmp_path, "inspect", "R-0001")
    assert code == wire.EXIT_OK and json.loads(out)["outcome"] == "complete"


def test_the_default_agents_load_and_research_runs_offline_on_a_world(tmp_path: Path) -> None:
    from aksha.app import DEFAULT_AGENTS, DEFAULT_EXTENSIONS

    sources = REPO / "fixtures" / "worlds" / "stillroom" / "sources"
    defaults = {"AKSHA_AGENTS": str(DEFAULT_AGENTS), "AKSHA_EXTENSIONS": str(DEFAULT_EXTENSIONS),
                "AKSHA_SOURCES": f"inputs={sources}"}  # fmt: skip
    code, out = aksha(tmp_path, "capabilities", **defaults)
    assert code == wire.EXIT_OK, out
    request = "Who sells camera-free occupancy sensing for offices, and at what price?"
    code, out = aksha(tmp_path, "run", request, "--agent", "research", "--session", "S-1",
                      **defaults)  # fmt: skip
    assert code == wire.EXIT_WAITING_INPUT, out
    interrupt = re.search(r"aksha answer (I-\d+)", out)
    assert interrupt is not None
    code, out = aksha(tmp_path, "answer", interrupt.group(1), "EU", **defaults)
    assert code == wire.EXIT_OK, out
    assert "research ended complete" in out
    store = open_store(tmp_path / "var" / "aksha.sqlite", FakeClock(START), writer=False)
    try:
        reads = [e for e in store.read("o1") if e.type == "read.recorded"]
    finally:
        store.close()
    assert reads, "the tools read the world's records through the configured source"


def test_the_patent_agent_runs_offline_on_a_world_up_to_its_approval(tmp_path: Path) -> None:
    from aksha.app import DEFAULT_AGENTS, DEFAULT_EXTENSIONS

    sources = REPO / "fixtures" / "worlds" / "stillroom" / "sources"
    defaults = {"AKSHA_AGENTS": str(DEFAULT_AGENTS), "AKSHA_EXTENSIONS": str(DEFAULT_EXTENSIONS),
                "AKSHA_SOURCES": f"inputs={sources}"}  # fmt: skip
    request = "Assess the four invention candidates and prepare the chosen one for counsel."
    code, out = aksha(tmp_path, "run", request, "--agent", "patent", "--session", "S-1",
                      **defaults)  # fmt: skip
    assert code == wire.EXIT_WAITING_INPUT and "missing detail" in out, out
    gap = re.search(r"aksha answer (I-\d+)", out)
    assert gap is not None
    detail = "0.5 m from the vent edge, so its air flow stays outside the footprint."
    code, out = aksha(tmp_path, "answer", gap.group(1), detail, **defaults)
    assert code == wire.EXIT_WAITING_INPUT and "candidate selection" in out, out
    choice = re.search(r"aksha answer (I-\d+)", out)
    assert choice is not None
    code, out = aksha(tmp_path, "answer", choice.group(1), "C1", **defaults)
    assert code == wire.EXIT_WAITING_APPROVAL, out
    store = open_store(tmp_path / "var" / "aksha.sqlite", FakeClock(START), writer=False)
    try:
        events = list(store.read("o1"))
    finally:
        store.close()
    started = next(e for e in events if e.type == "run.started")
    shown = [json.loads(e.payload["value"]["view"]["text"])["data"] for e in events
             if e.type == "step.completed" and e.payload.get("tool") == "read_disclosures"
             and e.payload.get("outcome") == "ok"]  # fmt: skip
    assert shown and all(d["as_of"] == started.at[:10] for d in shown)
    assert all(r["elapsed_days"] is not None for d in shown for r in d["rows"] if r["date"])
    questions = [e.payload for e in events if e.type == "interrupt.raised"]
    assert "None days" not in json.dumps(thaw(questions))


def test_stop_exits_six(tmp_path: Path) -> None:
    code, out = aksha(tmp_path, "run", "Ask first", "--session", "S-1")
    assert code == wire.EXIT_WAITING_INPUT
    code, out = aksha(tmp_path, "stop", "R-0001")
    assert code == wire.EXIT_STOPPED, out
    code, out = aksha(tmp_path, "stop", "R-9999")
    assert code == wire.EXIT_REFUSED


def test_headless_needs_a_sandbox_setting(tmp_path: Path) -> None:
    code, out = aksha(tmp_path, "run", "Write and publish", "--headless", "--session", "S-1")
    assert code == wire.EXIT_REFUSED and "sandbox_required" in out and "AKSHA_SANDBOX" in out
    code, out = aksha(tmp_path, "run", "Write a note", "--headless", "--session", "S-2",
                      AKSHA_SANDBOX="outer:os-jail")  # fmt: skip
    assert code == wire.EXIT_OK and "(headless)" in out, out


def test_a_retry_after_a_lost_receipt_reuses_the_pending_command_id(tmp_path: Path) -> None:
    argv = ("run", "Write a note", "--session", "S-1")
    pending = tmp_path / "var" / "aksha.pending.json"
    pending.parent.mkdir(parents=True)
    from aksha.interfaces.cli.pending import PendingIds, invocation_key

    held = PendingIds(pending).take(invocation_key("o1", list(("--user", "o1", *argv))))
    code, _ = aksha(tmp_path, *argv)
    assert code == wire.EXIT_OK
    store = open_store(tmp_path / "var" / "aksha.sqlite", FakeClock(START), writer=False)
    try:
        accepted = [e for e in store.read("o1") if e.type == "command.accepted"]
    finally:
        store.close()
    assert accepted[0].payload["command_id"] == held
    assert json.loads(pending.read_text()) == {}


def test_a_held_writer_lock_exits_eight_and_reads_still_work(tmp_path: Path) -> None:
    aksha(tmp_path, "run", "Write a note", "--session", "S-1")
    writer = open_store(tmp_path / "var" / "aksha.sqlite", FakeClock(START), writer=True)
    try:
        code, out = aksha(tmp_path, "run", "Write a note", "--session", "S-2")
        assert code == wire.EXIT_ENVIRONMENT and "locked" in out
        code, _ = aksha(tmp_path, "status")
        assert code == wire.EXIT_OK
    finally:
        writer.close()


def test_usage_errors_exit_two(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        aksha(tmp_path, "launch")
    assert raised.value.code == wire.EXIT_USAGE


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    port = _free_port()
    environ = {**os.environ, **env(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "aksha.interfaces.cli", "--user", "o1", "serve",
         "--port", str(port)],
        env=environ, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )  # fmt: skip
    server_file = tmp_path / "var" / "aksha.server.json"
    deadline = time.monotonic() + 30
    while not server_file.exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            out = proc.stdout.read().decode() if proc.stdout else ""
            proc.kill()
            proc.wait(timeout=15)
            if proc.stdout:
                proc.stdout.close()
            raise AssertionError(f"the server did not start: {out}")
        time.sleep(0.05)
    url = json.loads(server_file.read_text())["url"]
    from aksha.interfaces.cli.client import find_server

    while find_server(url, server_file) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        if proc.stdout:
            proc.stdout.close()


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_server_mode_sends_commands_to_the_server(tmp_path: Path, server: str) -> None:
    code, out = aksha(tmp_path, "run", "Ask first", "--session", "S-1")
    assert code == wire.EXIT_WAITING_INPUT, out
    interrupt = re.search(r"aksha answer (I-\d+)", out)
    assert interrupt is not None
    code, out = aksha(tmp_path, "answer", interrupt.group(1), "blue")
    assert code == wire.EXIT_OK, out
    code, out = aksha(tmp_path, "capabilities")
    assert code == wire.EXIT_OK and "SendMessage" in json.loads(out)["commands"]
    code, out = aksha(tmp_path, "status", "R-0001")  # a read beside the server
    assert code == wire.EXIT_OK
    code, out = aksha(
        tmp_path, "run", "Write", "--session", "S-2", AKSHA_SERVER_URL="http://127.0.0.1:9"
    )  # no server there: embedded
    assert code == wire.EXIT_ENVIRONMENT and "locked" in out


def test_a_retry_without_session_reuses_the_generated_session(tmp_path: Path) -> None:
    from aksha.interfaces.cli.pending import PendingIds, invocation_key

    argv = ["--user", "o1", "run", "Write a note"]
    key = invocation_key("o1", argv)
    pending = PendingIds(tmp_path / "var" / "aksha.pending.json")
    held_session = pending.take(key + "/session", lambda: "S-held")
    code, out = aksha(tmp_path, "run", "Write a note")
    assert code == wire.EXIT_OK and f"session {held_session}" in out and "ended complete" in out


def test_a_session_waiting_on_a_route_question_reports_it() -> None:
    from aksha.app import RuntimeConfig
    from aksha.interfaces.cli.main import Context, _report_session
    from aksha.runtime.session.views import SessionStatus, Waiting

    out = io.StringIO()
    ctx = Context(RuntimeConfig(), "o1", out, ["run", "x"])
    status = SessionStatus("S-1", None, (Waiting("I-0001", "route"),), 4)
    assert _report_session(ctx, status) == wire.EXIT_WAITING_INPUT
    assert "aksha answer I-0001 --agent" in out.getvalue()
    assert _report_session(ctx, SessionStatus("S-1", None, (), 4)) == wire.EXIT_OK
