"""Tier 3 in the agent loop (ADR-P26 D2 to D4, ADR-P12 decisions 2, 8, 9, T6, ADR-P28 D5).

A tool reaches the network only through ``ctx.http`` and only under its policy, each hop's
decision is on its ``step.completed``, and a refusal is an error the model reads and that
is never retried. An exec tool runs through ``ctx.exec`` in a box, the box's record is on
its ``step.completed``, and with no backend the gate refuses before any body runs. Every
exec of a session's runs names that session, so it runs in the session's one stateful
workspace (decision 37). The
bubblewrap test runs a real box and is skipped where the backend does not probe available.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aksha.adapters.sandbox.bwrap import BwrapSandbox
from aksha.runtime.canonical import thaw
from aksha.runtime.sandbox.types import SessionRef
from tests.support.loop import Recorder, call, finish_reply, tools_reply
from tests.support.sandbox import FakeSandbox
from tests.support.tier3 import OWNER, TierBench

BWRAP = BwrapSandbox()
BWRAP_PROBE = BWRAP.probe()


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Any]:
    benches: list[TierBench] = []

    def build(*calls: Any, **kw: Any) -> TierBench:
        model = Recorder([tools_reply(*calls), finish_reply()])
        b = TierBench(tmp_path / f"b{len(benches)}", model, **kw)
        benches.append(b)
        return b

    yield build
    for b in benches:
        b.close()


def test_an_allowed_fetch_records_its_egress_decision(make: Any) -> None:
    b = make(call("fetch_page", url="https://docs.example.org/a"))
    assert b.drive().outcome == "complete"
    (step,) = b.tool_steps("fetch_page")
    assert step.payload["outcome"] == "ok"
    (decision,) = thaw(step.payload["value"]["egress"])
    assert decision == {"policy": "web.docs", "url": "https://docs.example.org/a",
                        "host": "docs.example.org", "addresses": ["93.184.215.14"],
                        "allowed": True}  # fmt: skip
    assert b.transport.sent == [("https://docs.example.org/a", "93.184.215.14")]


def test_a_redirect_to_a_private_address_is_refused_recorded_and_never_retried(make: Any) -> None:
    b = make(call("fetch_page", url="https://docs.example.org/moved"))
    b.drive()
    (step,) = b.tool_steps("fetch_page")
    assert (step.payload["outcome"], step.payload["code"]) == ("error", "egress_refused")
    assert step.payload["attempts"] == 1  # the tool's retry_on names it; it is still not retried
    hops = step.payload["value"]["egress"]
    assert [(h["host"], h["allowed"]) for h in hops] == [("docs.example.org", True),
                                                         ("rebind.example.org", False)]  # fmt: skip
    assert hops[1]["reason"] == "private_address" and len(b.transport.sent) == 1


def test_a_tool_with_no_egress_policy_reaches_nothing(make: Any) -> None:
    b = make(call("peek", url="https://docs.example.org/a"))
    b.drive()
    (step,) = b.tool_steps("peek")
    assert step.payload["code"] == "egress_refused"
    assert step.payload["value"]["egress"][0]["reason"] == "no_egress_policy"
    assert b.transport.sent == []


def test_a_process_with_the_network_off_reaches_nothing(make: Any) -> None:
    b = make(call("fetch_page", url="https://docs.example.org/a"), network=False)
    b.drive()
    (step,) = b.tool_steps("fetch_page")
    assert step.payload["value"]["egress"][0]["reason"] == "network_off"


def test_an_exec_tool_with_no_backend_is_refused_by_the_gate_before_its_body(make: Any) -> None:
    b = make(call("run_script", code="print(1)"))
    b.drive()
    (step,) = b.tool_steps("run_script")
    assert (step.payload["outcome"], step.payload["code"]) == ("refused", "sandbox_unavailable")
    assert step.payload["layer"] == 7 and "exec" not in (step.payload.get("value") or {})


def test_an_exec_tool_runs_in_the_backend_and_its_record_is_on_the_step(make: Any) -> None:
    box = FakeSandbox()
    b = make(call("run_script", code="print(1)"), box=box)
    assert b.drive().outcome == "complete"
    (step,) = b.tool_steps("run_script")
    (record,) = step.payload["value"]["exec"]
    assert record["backend"] == "fake" and record["status"] == "ok"
    assert list(record["argv"]) == ["python3", "-c", "print(1)"]
    assert record["invocation"].endswith("#a1/x1") and record["policy_hash"].startswith("sha")
    assert box.requests[0].policy.network == "none"


@pytest.mark.skipif(not BWRAP_PROBE.available, reason=f"bwrap: {BWRAP_PROBE.reason}")
def test_an_exec_tool_runs_in_a_real_bubblewrap_box(make: Any) -> None:
    code = "import os; open('out/r.txt','w').write('r'); print(os.getcwd(), sorted(os.environ))"
    b = make(call("run_script", code=code), box=BWRAP)
    assert b.drive().outcome == "complete"
    (step,) = b.tool_steps("run_script")
    (record,) = step.payload["value"]["exec"]
    assert record["backend"] == "bwrap" and record["version"].startswith("bubblewrap")
    assert set(record["outputs"]) == {"out/r.txt"} and record["exit_code"] == 0
    assert "/work" in str(step.payload["value"]["view"])


def _session_run(root: Path, box: Any, session: str, *codes: str) -> list[tuple[Any, str]]:
    """One run in ``session`` (its own store under ``root``) running each script in turn.

    Each exec's record and the view its step showed the model.
    """
    replies = [tools_reply(call("run_script", n, code=c)) for n, c in enumerate(codes, 1)]
    b = TierBench(root, Recorder([*replies, finish_reply()]), box=box, session=session)
    try:
        assert b.drive().outcome == "complete"
        steps = b.tool_steps("run_script")
        return [(thaw(s.payload["value"]["exec"])[0], str(s.payload["value"].get("view")))
                for s in steps]  # fmt: skip
    finally:
        b.close()


def test_every_exec_names_its_runs_session(tmp_path: Path) -> None:
    box = FakeSandbox()
    records = _session_run(tmp_path / "a", box, "S-0001", "print(1)", "print(2)")
    records += _session_run(tmp_path / "b", box, "S-0002", "print(3)")
    assert [r.session for r in box.requests] == [SessionRef(OWNER, s) for s in
                                                 ("S-0001", "S-0001", "S-0002")]  # fmt: skip
    assert [r["session"]["session"] for r, _ in records] == ["S-0001", "S-0001", "S-0002"]


WRITE = "open('out/state.txt', 'w').write('from the first exec')"
READ = "import os; p = 'out/state.txt'; print(open(p).read() if os.path.exists(p) else 'none')"


@pytest.mark.skipif(not BWRAP_PROBE.available, reason=f"bwrap: {BWRAP_PROBE.reason}")
def test_one_session_keeps_one_workspace_across_execs_and_runs(tmp_path: Path) -> None:
    box = BwrapSandbox(workspaces=tmp_path / "sandbox")
    (w, _), (r, view) = _session_run(tmp_path / "a", box, "S-0001", WRITE, READ)
    assert "from the first exec" in view
    assert w["workspace"]["created"] is True and r["workspace"]["created"] is False
    assert w["workspace"]["folder"] == str(tmp_path / "sandbox" / OWNER / "S-0001" / "work")
    assert set(w["outputs"]) == {"out/state.txt"} and r["outputs"] == {}
    ((later, later_view),) = _session_run(tmp_path / "b", box, "S-0001", READ)
    assert "from the first exec" in later_view  # a later run of the session: kept
    assert later["workspace"]["folder"] == w["workspace"]["folder"]
    ((other, other_view),) = _session_run(tmp_path / "c", box, "S-0002", READ)
    assert "none" in other_view and other["workspace"]["folder"] != w["workspace"]["folder"]
