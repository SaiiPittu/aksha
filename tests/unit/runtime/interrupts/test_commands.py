"""The deciding commands (ADR-P17 D4, D5, D8, S1, S2): only a person resolves, once."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.runtime.interrupts.approvals import approvals, request_approval
from aksha.runtime.interrupts.commands import (
    ANSWER_MISSING,
    EXPLICIT_COMMAND_REQUIRED,
    NOT_AN_OPTION,
    answer,
    approve,
    edit,
    grant_budget,
    handoff,
    reject,
)
from aksha.runtime.interrupts.raising import ask_request, raised, route_request
from aksha.runtime.interrupts.reducers import interrupts, open_interrupts
from aksha.runtime.interrupts.types import DeclaredInterrupt, Refusal
from aksha.runtime.outbox.outbox import entries
from aksha.runtime.session.commands import ALREADY_DECIDED
from tests.support.approvals import DEFINITION, SEND, TOOLS
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


def _requested(b: Bench, approval: str = "A-0001") -> str:
    events = request_approval(b.view(), SEND, ARGS, approval, definition_hash=DEFINITION,
                              run="R-0001", instance="send")  # fmt: skip
    assert not isinstance(events, Refusal)
    b.store.append(b.owner, "U2", events)
    return approvals(b.view())[approval].signature


def _ask(b: Bench, ident: str, args: dict[str, object], kind: str = "input") -> None:
    declared = DeclaredInterrupt(kind, "gap")  # type: ignore[arg-type]
    request = ask_request(declared, args, run="R-0001", instance="collect[C1]")
    b.store.append(b.owner, "U2", (raised(request, ident),))


# --- answer ---------------------------------------------------------------------------


def test_answer_resolves_an_input_and_mints_a_statement(bench: Bench) -> None:
    _ask(bench, "I-0001", {"question": "which field?"})
    receipt = answer(bench.store, bench.owner, "I-0001", by="person", text="the second")
    assert receipt.accepted
    i = interrupts(bench.view())["I-0001"]
    assert i.state == "resolved" and dict(i.resolution["answers"]) == {"q1": "the second"}  # type: ignore[index]
    assert bench.types()[-2:] == ["interrupt.resolved", "excerpts.minted"]


def test_two_questions_get_two_answers_and_two_spans(bench: Bench) -> None:
    """S2: one answer per question, each its own statement span."""
    _ask(bench, "I-0001", {"questions": [{"id": "g1", "text": "a?"}, {"id": "g2", "text": "b?"}]})
    one = answer(bench.store, bench.owner, "I-0001", by="person", text="both")
    assert one.refused == ANSWER_MISSING
    ok = answer(bench.store, bench.owner, "I-0001", by="person",
                answers={"g1": "first", "g2": "second"})  # fmt: skip
    assert ok.accepted
    minted = [e for e in bench.events() if e.type == "excerpts.minted"]
    spans = [s for e in minted for s in e.payload["spans"]]
    assert [s["text"] for s in spans] == ["first", "second"]
    assert len({s["span_id"] for s in spans}) == 2


def test_a_choice_takes_only_a_listed_option(bench: Bench) -> None:
    _ask(bench, "I-0001", {"question": "which?", "options": ["C1", "C2"]}, kind="choice")
    assert answer(bench.store, bench.owner, "I-0001", by="p", text="C9").refused == NOT_AN_OPTION
    assert answer(bench.store, bench.owner, "I-0001", by="p", text="C2").accepted


def test_a_second_answer_is_already_decided(bench: Bench) -> None:
    _ask(bench, "I-0001", {"question": "which?"})
    assert answer(bench.store, bench.owner, "I-0001", by="a", text="x", command_id="c1").accepted
    again = answer(bench.store, bench.owner, "I-0001", by="b", text="y", command_id="c2")
    assert again.refused == ALREADY_DECIDED


def test_free_text_cannot_approve(bench: Bench) -> None:
    """A message "approve it" resolves no approval, budget or review interrupt (IE-10)."""
    _requested(bench)
    receipt = answer(bench.store, bench.owner, "A-0001", by="person", text="approve it")
    assert receipt.refused == EXPLICIT_COMMAND_REQUIRED
    assert approvals(bench.view())["A-0001"].state == "requested"
    assert "approval.granted" not in bench.types()


def test_route_is_session_scoped_and_answered_by_an_agent(bench: Bench) -> None:
    request = route_request("S-1", "which agent?", ("alpha", "beta"))
    bench.store.append(bench.owner, "U2", (raised(request, "I-0001"),))
    i = interrupts(bench.view())["I-0001"]
    assert i.scope == "session" and i.run is None
    assert answer(bench.store, bench.owner, "I-0001", by="p", text="beta").accepted


# --- approve, reject, edit ------------------------------------------------------------


def test_approve_needs_the_shown_signature(bench: Bench) -> None:
    sig = _requested(bench)
    wrong = approve(bench.store, bench.owner, "A-0001", "v1:" + "0" * 64, by="person")
    assert wrong.refused == "signature_mismatch"
    assert approve(bench.store, bench.owner, "A-0001", sig, by="person").accepted
    view = bench.view()
    assert approvals(view)["A-0001"].state == "granted"
    assert not interrupts(view)["A-0001"].is_open


def test_second_decision_refused(bench: Bench) -> None:
    sig = _requested(bench)
    assert approve(bench.store, bench.owner, "A-0001", sig, by="a", command_id="k1").accepted
    second = approve(bench.store, bench.owner, "A-0001", sig, by="b", command_id="k2")
    assert second.refused == ALREADY_DECIDED
    assert bench.types().count("approval.granted") == 1
    late = reject(bench.store, bench.owner, "A-0001", by="b", command_id="k3")
    assert late.refused == ALREADY_DECIDED


def test_approve_refused_while_a_target_is_changing(tmp_path: Path) -> None:
    """S1: no grant while the packet is dirty (an input of an input changed)."""
    from aksha.runtime.invalidation.commands import apply_source_change
    from tests.support.invalidation import FakeSource, ref

    b = Bench(tmp_path)
    try:
        b.start("R-0001")
        b.save("R-0001", "p.form/C1", {"names": 1}, reads=[record("K")])
        b.save("R-0001", "p.packet/C1", {"forms": 1}, reads=[ref("p.form/C1@v1").as_input()])
        sig = _requested(b)
        source = FakeSource()
        source.change(b.owner, {"refs/K": "2"})
        apply_source_change(b.store, b.owner, [source], "cs1")
        assert "approval.revoked" not in b.types()  # dirty is not confirmed
        receipt = approve(b.store, b.owner, "A-0001", sig, by="person")
        assert receipt.refused == "target_changing"
    finally:
        b.close()


def test_request_on_an_invalidated_target_is_revoked_with_its_interrupt(bench: Bench) -> None:
    """ADR-P05 D9: no approval request stays live on a target confirmed invalidated."""
    sig = _requested(bench)
    from aksha.runtime.invalidation.commands import apply_source_change
    from tests.support.invalidation import FakeSource

    source = FakeSource()
    source.change(bench.owner, {"refs/K": "2"})
    apply_source_change(bench.store, bench.owner, [source], "cs1")
    view = bench.view()
    assert approvals(view)["A-0001"].state == "revoked"
    i = interrupts(view)["A-0001"]
    assert (i.state, i.void_reason) == ("voided", "target_invalidated")
    assert open_interrupts(view, "R-0001", "send") == ()  # it no longer holds the instance
    receipt = approve(bench.store, bench.owner, "A-0001", sig, by="person")
    assert receipt.refused == ALREADY_DECIDED


def test_reject_changes_nothing(bench: Bench) -> None:
    _requested(bench)
    before = [e.type for e in bench.events()]
    assert reject(bench.store, bench.owner, "A-0001", by="person", note="no").accepted
    after = [e.type for e in bench.events()]
    assert after[len(before) :] == ["command.accepted", "approval.rejected", "interrupt.resolved"]
    assert approvals(bench.view())["A-0001"].state == "rejected"


def test_edit_needs_fresh_approval(bench: Bench) -> None:
    sig = _requested(bench)
    bench.save("R-0001", "p.note/R2", {"address": "two"})
    receipt = edit(bench.store, bench.owner, "A-0001", {"to": "R2"}, tools=TOOLS, ids=bench.ids)
    assert receipt.accepted
    view = bench.view()
    old, new = approvals(view)["A-0001"], approvals(view)["A-0002"]
    assert old.state == "voided" and old.void_reason == "replaced_by_edit"
    assert new.state == "requested" and new.replaces == "A-0001"
    assert new.signature != sig and new.body.facts["recipient"] == "R2"
    assert [i.id for i in open_interrupts(view, "R-0001")] == ["A-0002"]


# --- grant_budget ---------------------------------------------------------------------


def test_grant_budget_answers_only_a_budget_interrupt(bench: Bench) -> None:
    from aksha.runtime.interrupts.types import InterruptRequest

    budget = InterruptRequest("budget", run="R-0001", extra={"limit": "model_turns"})
    bench.store.append(bench.owner, "U3", (raised(budget, "budget-1"),))
    _requested(bench)
    wrong = grant_budget(bench.store, bench.owner, "A-0001", {"model_turns": 5}, by="p")
    assert wrong.refused == "not_a_budget_interrupt"
    ok = grant_budget(bench.store, bench.owner, "budget-1", {"model_turns": 5}, by="p")
    assert ok.accepted
    assert bench.types()[-2:] == ["budget.granted", "interrupt.resolved"]


# --- handoff --------------------------------------------------------------------------


def test_send_without_approval_refused(bench: Bench) -> None:
    _requested(bench)
    receipt = handoff(bench.store, bench.owner, "A-0001", tools=TOOLS, definition_hash=DEFINITION)
    assert receipt.refused == "approval_not_granted"
    assert entries(bench.view()) == {}


def test_handoff_writes_one_entry_and_uses_the_approval(bench: Bench) -> None:
    sig = _requested(bench)
    approve(bench.store, bench.owner, "A-0001", sig, by="person")
    first = handoff(bench.store, bench.owner, "A-0001", tools=TOOLS, definition_hash=DEFINITION)
    assert first.accepted
    (entry,) = entries(bench.view()).values()
    assert entry.approval_id == "A-0001"
    assert approvals(bench.view())["A-0001"].state == "used"
    again = handoff(bench.store, bench.owner, "A-0001", tools=TOOLS, definition_hash=DEFINITION,
                    command_id="other")  # fmt: skip
    assert again.refused == "approval_used"


def test_fact_change_needs_fresh_approval(bench: Bench) -> None:
    sig = _requested(bench)
    approve(bench.store, bench.owner, "A-0001", sig, by="person")
    bench.save("R-0001", "p.note/R1", {"address": "moved"})
    receipt = handoff(bench.store, bench.owner, "A-0001", tools=TOOLS, definition_hash=DEFINITION)
    assert receipt.refused == "signature_mismatch"
    assert entries(bench.view()) == {}
