"""Raising an interrupt (ADR-P17 D1, D2, S2, S5): one fact, in the scope of its envelope."""

from __future__ import annotations

import pytest

from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue
from aksha.runtime.interrupts.raising import SINGLE_QUESTION, ask_request, raised, route_request
from aksha.runtime.interrupts.types import DeclaredInterrupt, InterruptRequest, Question

CORE = Catalogue(CORE_EVENT_TYPES)


def test_ask_with_one_question() -> None:
    request = ask_request(DeclaredInterrupt("input", "scope"), {"question": "which region?"},
                          run="R-1", instance="scope")  # fmt: skip
    event = raised(request, "I-0001", step="R-1/scope/t01/c01")
    assert (event.run, event.instance, event.session) == ("R-1", "scope", None)
    assert event.payload == {
        "interrupt": "I-0001", "kind": "input", "label": "scope",
        "questions": [{"id": SINGLE_QUESTION, "text": "which region?"}],
    }  # fmt: skip
    CORE.validate_payload(event.type, event.payload)


def test_ask_with_several_questions_keeps_each() -> None:
    """S2: two gaps are two questions, so each gets its own answer."""
    args = {"questions": [{"id": "g1", "text": "a?"}, {"id": "g2", "text": "b?"}]}
    request = ask_request(DeclaredInterrupt("input", "gap"), args, run="R-1", instance="x[C1]")
    assert request.questions == (Question("g1", "a?"), Question("g2", "b?"))


def test_a_choice_lists_options() -> None:
    args = {"question": "which?", "options": ["C1", "C2"]}
    request = ask_request(DeclaredInterrupt("choice", "pick"), args, run="R-1", instance="pick")
    assert raised(request, "I-0002").payload["options"] == ["C1", "C2"]
    with pytest.raises(ValueError, match="lists its options"):
        ask_request(DeclaredInterrupt("choice", "pick"), {"question": "?"}, run="R", instance="i")


def test_route_is_scoped_to_a_session() -> None:
    event = raised(route_request("S-1", "which agent?", ("alpha", "beta")), "I-0003")
    assert (event.run, event.session) == (None, "S-1")
    assert event.payload["kind"] == "route"
    CORE.validate_payload(event.type, event.payload)


@pytest.mark.parametrize(
    "kw",
    [
        {"kind": "route", "run": "R-1", "options": ("a",)},
        {"kind": "route", "options": ("a",)},
        {"kind": "approval"},
        {"kind": "input", "run": "R-1"},
        {"kind": "approval", "instance": "x"},
        {"kind": "input", "run": "R-1", "questions": (Question("q", "a"), Question("q", "b"))},
    ],
)
def test_a_request_out_of_scope_is_refused(kw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        InterruptRequest(**kw)  # type: ignore[arg-type]


def test_extra_cannot_overwrite_the_envelope_fields() -> None:
    request = InterruptRequest("budget", run="R-1", extra={"kind": "input"})
    with pytest.raises(ValueError, match="every interrupt has"):
        raised(request, "budget-1")
