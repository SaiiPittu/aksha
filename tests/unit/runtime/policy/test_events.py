from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.canonical import sha256_hex
from aksha.runtime.policy.events import (
    args_hash,
    boundary_of,
    decision_event,
    tool_calls_used,
)
from aksha.runtime.policy.types import Allow, Refuse
from aksha.runtime.storage.errors import AlreadyRecorded
from aksha.runtime.tools.types import ToolCall
from tests.support.gate import append_decision

CALL = ToolCall("fetch", {"url": "u"}, position=2)
REFUSED = Refuse("not_allowed", 2, "fetch is not in this agent's list", counted=True)
STEP = {"run": "R-1", "instance": "gather", "step": "R-1/gather/t01/c02"}


def test_args_hash_is_sha256_of_the_canonical_arguments() -> None:
    assert args_hash({"b": 1, "a": 2}) == "sha256:" + sha256_hex({"a": 2, "b": 1})


def test_an_allow_is_a_step_started_in_the_step_intent_boundary() -> None:
    e = decision_event(Allow("dispatch"), CALL, **STEP)
    assert e.type == "step.started" and boundary_of(Allow("dispatch")) == "U2a"
    assert dict(e.payload) == {
        "kind": "tool",
        "tool": "fetch",
        "args_hash": args_hash(CALL.arguments),
        "position": 2,
        "route": "dispatch",
        "counted": True,
    }
    assert (e.run, e.instance, e.step) == ("R-1", "gather", STEP["step"])


def test_a_refusal_is_one_step_completed_with_its_reason() -> None:
    e = decision_event(REFUSED, CALL, **STEP)
    assert e.type == "step.completed" and boundary_of(REFUSED) == "U2"
    assert e.payload["outcome"] == "refused"
    assert (e.payload["code"], e.payload["layer"], e.payload["counted"]) == ("not_allowed", 2, True)
    assert e.payload["message"] == REFUSED.message
    assert e.payload["detail"] == []


def test_record_decision_appends_once_per_step(tmp_path: Path, clock: FakeClock) -> None:
    store = open_store(tmp_path / "s.sqlite", clock, writer=True)
    try:
        stored = append_decision(store, "owner-1", REFUSED, CALL, **STEP)
        assert stored.seq == 1 and stored.dedup_key == f"sdone:{STEP['step']}"
        with pytest.raises(AlreadyRecorded):
            append_decision(store, "owner-1", REFUSED, CALL, **STEP)
        assert [e.type for e in store.read("owner-1")] == ["step.completed"]
    finally:
        store.close()


def test_tool_calls_used_counts_allows_and_counted_refusals_only(
    tmp_path: Path, clock: FakeClock
) -> None:
    store = open_store(tmp_path / "s.sqlite", clock, writer=True)
    try:
        decisions: list[Allow | Refuse] = [
            Allow("dispatch"),
            REFUSED,
            Refuse("budget_exhausted", 0, "spent", counted=False),
            Allow("reserved", counted=False),
        ]
        for i, d in enumerate(decisions):
            append_decision(
                store, "o", d, CALL, run="R-1", instance="i", step=f"R-1/i/t01/c{i:02d}"
            )
        assert tool_calls_used(store.read("o")) == 2
    finally:
        store.close()
