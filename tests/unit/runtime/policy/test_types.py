from dataclasses import FrozenInstanceError
from typing import get_args

import pytest

from aksha.runtime.policy.types import (
    LAYERS,
    MATCHERS,
    REFUSAL_CODES,
    ROUTES,
    Allow,
    ArgumentRule,
    Matcher,
    MeterReading,
    Permissions,
    RefusalCode,
    Refuse,
    Route,
    SourceVersion,
    StopPolicyNarrowing,
)


def test_matchers_routes_and_codes_are_closed_sets() -> None:
    assert (
        frozenset(get_args(Matcher))
        == MATCHERS
        == {
            "path_under",
            "path_not_under",
            "enum",
            "id_in_source",
            "max_length",
        }
    )
    assert (
        frozenset(get_args(Route))
        == ROUTES
        == {
            "dispatch",
            "outbox",
            "approval",
            "model",
            "reserved",
        }
    )
    assert frozenset(get_args(RefusalCode)) == REFUSAL_CODES
    assert "gate_error" in REFUSAL_CODES and "budget_exhausted" in REFUSAL_CODES


def test_layers_name_the_fixed_order_zero_to_seven() -> None:
    assert list(LAYERS) == list(range(8))
    assert LAYERS[0] == "budget" and LAYERS[6] == "argument rules"


def test_argument_rule_names_itself_for_a_refusal() -> None:
    rule = ArgumentRule("write_file", "/path", "path_not_under", ".git", "never inside .git")
    assert rule.label == "write_file/path path_not_under '.git'"


def test_permissions_allow_only_what_they_list() -> None:
    p = Permissions(frozenset({"fetch"}))
    assert p.allows("fetch") and not p.allows("send")
    assert p.rules == ()
    assert p.rules_for("fetch") == ()


def test_permissions_rules_for_one_tool() -> None:
    a = ArgumentRule("send", "/to", "enum", ("x",), "r")
    b = ArgumentRule("fetch", "/url", "max_length", 10, "r")
    assert Permissions(frozenset({"send", "fetch"}), (a, b)).rules_for("send") == (a,)


def test_no_narrowing_by_default() -> None:
    n = StopPolicyNarrowing()
    assert (n.tools, n.by) == (None, None)
    assert n.keeps("anything")
    plan = StopPolicyNarrowing(frozenset({"fetch"}), by="plan")
    assert plan.keeps("fetch") and not plan.keeps("send")


def test_meter_reading_is_spent_when_any_dimension_is() -> None:
    assert not MeterReading().spent
    assert MeterReading(frozenset({"tool_calls"})).spent


def test_source_version_holds_ids() -> None:
    s = SourceVersion("people", "v1", frozenset({"P-1"}))
    assert s.holds("P-1") and not s.holds("P-9")


def test_allow_and_refuse_are_frozen_and_counted_by_default() -> None:
    allow = Allow("dispatch")
    assert allow.counted
    refuse = Refuse("not_allowed", 2, "no", counted=True)
    assert refuse.detail == ()
    with pytest.raises(FrozenInstanceError):
        refuse.layer = 3  # type: ignore[misc]
