"""The two budget facts (``budgets/facts.py``) are valid against the core catalogue."""

from __future__ import annotations

import pytest

from aksha.runtime.budgets.facts import budget_granted, budget_reached
from aksha.runtime.budgets.meter import Meter
from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue
from aksha.runtime.events.errors import PayloadInvalid

CATALOGUE = Catalogue(CORE_EVENT_TYPES)


def test_budget_reached_names_the_first_spent_dimension() -> None:
    m = Meter({"model_turns": 3, "tool_calls": 40}, {"model_turns": 25, "tool_calls": 40})
    event = budget_reached("R-0001", "I-0001", m)
    assert dict(event.payload) == {
        "interrupt_id": "I-0001", "limit": "tool_calls", "used": 40, "allowed": 40,
        "partial": True,
    }  # fmt: skip
    CATALOGUE.validate_payload(event.type, event.payload)
    CATALOGUE.check_boundary(event.type, "U3")
    key = CATALOGUE.render_dedup_key(event.type, event.context(), event.payload)
    assert key == "breach:R-0001/I-0001"


def test_budget_reached_needs_a_spent_dimension() -> None:
    with pytest.raises(ValueError, match="spent"):
        budget_reached("R-0001", "I-0001", Meter({"tool_calls": 1}, {"tool_calls": 2}))


def test_budget_granted_is_a_command_fact_with_one_key_per_interrupt() -> None:
    event = budget_granted("R-0001", "I-0001", "person", {"tool_calls": 10})
    CATALOGUE.validate_payload(event.type, event.payload)
    CATALOGUE.check_boundary(event.type, "U4")
    key = CATALOGUE.render_dedup_key(event.type, event.context(), event.payload)
    assert key == "bgrant:R-0001/I-0001"


@pytest.mark.parametrize("extra", [{}, {"tool_calls": 0}, {"seconds": 5}])
def test_budget_granted_refuses_an_empty_or_unknown_grant(extra: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="grant"):
        budget_granted("R-0001", "I-0001", "person", extra)  # type: ignore[arg-type]


def test_the_catalogue_refuses_an_unknown_dimension_in_a_grant() -> None:
    payload = {"interrupt_id": "I", "by": "p", "extra": {"seconds": 5}}
    with pytest.raises(PayloadInvalid):
        CATALOGUE.validate_payload("budget.granted", payload)
