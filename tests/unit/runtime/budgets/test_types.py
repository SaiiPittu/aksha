from aksha.runtime.budgets.types import Backoff, Budget, Limits


def test_budget_narrow_takes_the_field_wise_minimum() -> None:
    a = Budget(model_turns=10, tool_calls=40, active_ms=1_000, tokens=None)
    b = Budget(model_turns=25, tool_calls=5, active_ms=2_000, tokens=300)
    assert a.narrow(b) == Budget(model_turns=10, tool_calls=5, active_ms=1_000, tokens=300)


def test_limits_defaults_follow_the_adr() -> None:
    lim = Limits()
    assert (lim.turn_limit, lim.stop_veto_limit, lim.restart_limit) == (10, 3, 3)
    assert lim.backoff == Backoff()
