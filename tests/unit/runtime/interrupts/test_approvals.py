"""Approvals (ADR-P17 D5 to D8): the signature, the generic body, the state and the re-check."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.interrupts.approvals import (
    DEFINITION_CHANGED,
    NOT_GRANTED,
    SIGNATURE_MISMATCH,
    STALE_TARGET,
    approval_signature,
    approvals,
    build_body,
    request_approval,
    verify_for_execution,
)
from aksha.runtime.interrupts.reducers import interrupts
from aksha.runtime.interrupts.types import ActionBody, Refusal
from tests.support.approvals import DEFINITION, SEND, body
from tests.support.invalidation import Bench, record

ARGS = {"packet": "p.packet/C1@v1", "to": "R1"}


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    b = Bench(tmp_path)
    b.start("R-0001")
    b.save("R-0001", "p.packet/C1", {"forms": 1}, reads=[record("K")])
    b.save("R-0001", "p.note/R1", {"address": "one"})
    yield b
    b.close()


def _request(b: Bench, approval: str = "A-0001") -> tuple[NewEvent, ...]:
    events = request_approval(b.view(), SEND, ARGS, approval, definition_hash=DEFINITION,
                              run="R-0001", instance="send")  # fmt: skip
    assert not isinstance(events, Refusal)
    b.store.append(b.owner, "U2", events)
    return events


def _grant(b: Bench, approval: str = "A-0001") -> None:
    a = approvals(b.view())[approval]
    payload = {"approval": approval, "signature": a.signature, "by": "person",
               "targets": list(a.body.target_refs())}  # fmt: skip
    b.store.append(b.owner, "U4", (NewEvent("approval.granted", payload, run="R-0001"),))


def test_signature_is_version_prefixed_and_covers_every_field() -> None:
    base = body()
    sig = approval_signature(base)
    assert sig.startswith("v1:") and len(sig) == 3 + 64
    assert approval_signature(body()) == sig  # deterministic
    for changed in (
        replace(base, tool="other"),
        replace(base, canonical_args='{"a":1}'),
        replace(base, targets=(("x@v1", "sha256:1"),)),
        replace(base, idempotency_key="send:k2"),
        replace(base, definition_hash="sha256:" + "1" * 64),
        replace(base, facts={"head": "abc"}),
    ):
        assert approval_signature(changed) != sig


def test_body_is_built_from_the_rule(bench: Bench) -> None:
    built = build_body(bench.view(), SEND, ARGS, definition_hash=DEFINITION)
    assert isinstance(built, ActionBody)
    assert built.tool == "send" and built.owner == bench.owner
    assert built.target_refs() == ("p.packet/C1@v1",)
    assert built.targets[0][1].startswith("sha256:")
    assert dict(built.facts) == {"recipient": "R1", "recipient_version": "p.note/R1@v1"}
    assert dict(built.args()) == ARGS
    assert ActionBody.from_json(built.as_json()) == built


def test_body_refused_on_an_unknown_target(bench: Bench) -> None:
    built = build_body(bench.view(), SEND, {**ARGS, "packet": "p.packet/C9@v1"},
                       definition_hash=DEFINITION)  # fmt: skip
    assert isinstance(built, Refusal) and built.code == STALE_TARGET


def test_request_writes_one_request_and_its_interrupt_under_one_id(bench: Bench) -> None:
    requested, raised = _request(bench)
    assert requested.type == "approval.requested" and raised.type == "interrupt.raised"
    view = bench.view()
    a = approvals(view)["A-0001"]
    assert a.state == "requested" and a.instance == "send"
    i = interrupts(view)["A-0001"]
    assert i.kind == "approval" and i.is_open and i.payload["signature"] == a.signature


def test_states_follow_the_diagram(bench: Bench) -> None:
    _request(bench)
    _grant(bench)
    assert approvals(bench.view())["A-0001"].state == "granted"
    stray = {"approval": "A-0002", "signature": "v1:" + "0" * 64, "by": "x"}
    bench.store.append(bench.owner, "U4", (NewEvent("approval.rejected", stray),))
    assert "A-0002" not in approvals(bench.view())  # a decision with no request is ignored


def test_verify_refuses_until_granted(bench: Bench) -> None:
    _request(bench)
    found = verify_for_execution(bench.view(), "A-0001", SEND, definition_hash=DEFINITION)
    assert isinstance(found, Refusal) and found.code == NOT_GRANTED


def test_verify_passes_then_refuses_a_changed_fact(bench: Bench) -> None:
    _request(bench)
    _grant(bench)
    ok = verify_for_execution(bench.view(), "A-0001", SEND, definition_hash=DEFINITION)
    assert isinstance(ok, ActionBody)
    bench.save("R-0001", "p.note/R1", {"address": "two"})  # the recipient record changed
    found = verify_for_execution(bench.view(), "A-0001", SEND, definition_hash=DEFINITION)
    assert isinstance(found, Refusal) and found.code == SIGNATURE_MISMATCH


def test_verify_refuses_a_changed_definition(bench: Bench) -> None:
    _request(bench)
    _grant(bench)
    found = verify_for_execution(bench.view(), "A-0001", SEND,
                                 definition_hash="sha256:" + "9" * 64)  # fmt: skip
    assert isinstance(found, Refusal) and found.code == DEFINITION_CHANGED
