from aksha.runtime.budgets.types import Backoff, Budget, Limits


def test_budget_narrow_takes_the_field_wise_minimum() -> None:
    a = Budget(model_turns=10, tool_calls=40, active_ms=1_000, tokens=None)
    b = Budget(model_turns=25, tool_calls=5, active_ms=2_000, tokens=300)
    assert a.narrow(b) == Budget(model_turns=10, tool_calls=5, active_ms=1_000, tokens=300)


def test_limits_defaults_follow_the_adr() -> None:
    lim = Limits()
    assert (lim.turn_limit, lim.stop_veto_limit, lim.restart_limit) == (10, 3, 3)
    assert lim.backoff == Backoff()


def test_budget_payload_records_only_capped_dimensions() -> None:
    b = Budget(model_turns=3, tool_calls=4, active_ms=5, tokens=600)
    assert b.as_payload() == {"model_turns": 3, "tool_calls": 4, "active_ms": 5, "tokens": 600}
    assert Budget.from_payload(b.as_payload()) == b
    assert b.cap("cost_micro_usd") is None and b.cap("tokens") == 600


def test_budget_from_payload_keeps_defaults_for_the_always_capped_three() -> None:
    assert Budget.from_payload({"tool_calls": 2}) == Budget(tool_calls=2)
    assert Budget.from_payload(None) == Budget()


def test_budget_refuses_a_negative_cap() -> None:
    import pytest

    with pytest.raises(ValueError, match="not negative"):
        Budget(tool_calls=-1)
