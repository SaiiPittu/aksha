"""Headless runs, option B (ADR-P11 D12, decision 26): asks become allows, denies hold."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.policy.gate import check, resolve_ask
from aksha.runtime.policy.headless import (
    SANDBOX_REQUIRED,
    headless_start_refusal,
    started_headless,
)
from aksha.runtime.policy.types import Allow, GateInput, MeterReading, Refuse, StopPolicyNarrowing
from aksha.runtime.sandbox.types import Availability
from tests.support.gate import Spy, agent, gate_input, registry

SPY = Spy()
A = agent(SPY)
R = registry(A)
BOX = Availability("docker", "container", available=True)


def test_resolve_ask_turns_only_the_approval_route() -> None:
    send = R.get("send").spec
    fetch = R.get("fetch").spec
    assert resolve_ask(Allow("approval"), True, send) == Allow("outbox")
    assert resolve_ask(Allow("approval"), False, send) == Allow("approval")
    assert resolve_ask(Allow("dispatch"), True, fetch) == Allow("dispatch")
    assert resolve_ask(Allow("approval", counted=False), True, fetch) == Allow("dispatch", False)


def test_headless_never_raises_approval_interrupt() -> None:
    attended = gate_input(A, "act", "send")
    assert check(attended, R) == Allow("approval")
    assert check(replace(attended, headless=True), R) == Allow("outbox")


def _deny_cells() -> list[tuple[str, GateInput]]:
    narrowed = StopPolicyNarrowing(frozenset({"save_artifact"}), by="plan")
    return [
        ("not_allowed", gate_input(replace(A, tools=()), "gather", "fetch")),
        ("not_in_task", gate_input(A, "gather", "send")),
        ("narrowed", gate_input(A, "act", "write_file", narrowing=narrowed)),
        ("argument_rule", gate_input(A, "act", "write_file", {"path": ".git/x", "text": "x"})),
        ("recipient_not_listed", gate_input(A, "act", "send",
                                            {"document": "doc:1@v1", "recipient_id": "out"})),
        ("sandbox_unavailable", gate_input(A, "build", "run", sandbox=None)),
        ("budget_exhausted", gate_input(A, "act", "send",
                                        meter=MeterReading(frozenset({"tool_calls"})))),
    ]  # fmt: skip


@pytest.mark.parametrize(("code", "g"), _deny_cells())
def test_headless_deny_still_refuses(code: str, g: GateInput) -> None:
    attended, headless = check(g, R), check(replace(g, headless=True), R)
    assert isinstance(attended, Refuse) and attended.code == code
    assert headless == attended
    assert SPY.calls == []


def test_headless_requires_sandbox() -> None:
    assert headless_start_refusal(True, None) == SANDBOX_REQUIRED
    down = Availability("docker", "container", available=False, reason="daemon down")
    assert headless_start_refusal(True, down) == SANDBOX_REQUIRED
    assert headless_start_refusal(True, BOX) is None
    assert headless_start_refusal(False, None) is None


def test_the_flag_is_read_from_run_started(tmp_path: Any) -> None:
    from aksha.adapters.clock.fake import FakeClock
    from aksha.adapters.storage.sqlite import open_store
    from aksha.runtime.projections.view import load_view
    from tests.support.events import START
    from tests.support.steps import recovery_catalogue

    store = open_store(tmp_path / "a.sqlite", FakeClock(START), writer=True,
                       catalogue=recovery_catalogue())  # fmt: skip
    store.append("o", "U1", (
        NewEvent("run.started", {"agent": "a", "cause": "request", "headless": True}, run="R-1"),
        NewEvent("run.started", {"agent": "a", "cause": "request"}, run="R-2"),
    ))  # fmt: skip
    view = load_view(store, "o")
    assert started_headless(view, "R-1") and not started_headless(view, "R-2")
    assert not started_headless(view, "R-3")
    store.close()
