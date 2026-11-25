"""One test per layer of ``check`` (ADR-P11 D3), in the fixed order, plus fail-closed."""

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from aksha.runtime.policy.errors import RuleInvalid
from aksha.runtime.policy.gate import check, permitted
from aksha.runtime.policy.permissions import ceiling_of
from aksha.runtime.policy.types import (
    Allow,
    ArgumentRule,
    MeterReading,
    Permissions,
    Refuse,
    StopPolicyNarrowing,
)
from aksha.runtime.sandbox.types import Availability
from aksha.runtime.tools.registry import ToolRegistry
from tests.support.gate import NO_GIT, SEND_TO_LISTED, Spy, agent, gate_input, registry

A = agent(Spy())
R = registry(A)


def _refuse(result: Allow | Refuse) -> Refuse:
    assert isinstance(result, Refuse), result
    return result


def test_a_listed_tool_with_good_arguments_is_allowed_and_routed() -> None:
    assert check(gate_input(A, "gather", "fetch"), R) == Allow("dispatch")
    assert check(gate_input(A, "act", "post"), R) == Allow("outbox")
    assert check(gate_input(A, "act", "send"), R) == Allow("approval")
    assert check(gate_input(A, "build", "classify"), R) == Allow("model")
    assert check(gate_input(A, "build", "run"), R) == Allow("dispatch")
    assert check(gate_input(A, "idle", "finish"), R) == Allow("reserved")


def test_layer_0_budget_refusal_is_not_counted() -> None:
    g = gate_input(A, "gather", "fetch", meter=MeterReading(frozenset({"tool_calls"})))
    r = _refuse(check(g, R))
    assert (r.code, r.layer, r.counted) == ("budget_exhausted", 0, False)
    assert r.detail == ("tool_calls",)


def test_layer_1_unknown_tool() -> None:
    r = _refuse(check(gate_input(A, "gather", "nope"), R))
    assert (r.code, r.layer, r.counted) == ("unknown_tool", 1, True)


def test_layer_2_a_tool_outside_the_agents_list() -> None:
    narrow = replace(A, tools=())  # registered, but this agent lists nothing
    r = _refuse(check(gate_input(narrow, "gather", "fetch"), R))
    assert (r.code, r.layer) == ("not_allowed", 2)


def test_layer_2_a_tool_outside_the_run_ceiling() -> None:
    g = gate_input(A, "gather", "fetch", ceiling=Permissions(frozenset({"save_artifact"})))
    assert (_refuse(check(g, R)).code, _refuse(check(g, R)).layer) == ("not_allowed", 2)


def test_layer_2_implicit_tools_are_inside_every_ceiling() -> None:
    g = gate_input(A, "idle", "finish", ceiling=Permissions(frozenset()))
    assert check(g, R) == Allow("reserved")


def test_layer_3_a_tool_outside_the_tasks_list() -> None:
    r = _refuse(check(gate_input(A, "gather", "send"), R))
    assert (r.code, r.layer) == ("not_in_task", 3)


def test_layer_3_ask_needs_a_declared_interrupt() -> None:
    assert _refuse(check(gate_input(A, "gather", "ask"), R)).code == "not_in_task"
    assert check(gate_input(A, "act", "ask"), R) == Allow("reserved")


def test_layer_4_narrowing_names_the_stop_policy() -> None:
    plan = StopPolicyNarrowing(frozenset({"save_artifact"}), by="plan")
    r = _refuse(check(gate_input(A, "act", "write_file", narrowing=plan), R))
    assert (r.code, r.layer, r.detail) == ("narrowed", 4, ("plan",))
    assert "plan" in r.message


def test_layer_4_reserved_tools_are_never_narrowed_away() -> None:
    forced = StopPolicyNarrowing(frozenset({"fetch"}), by="forced_tool")
    assert check(gate_input(A, "act", "ask", narrowing=forced), R) == Allow("reserved")
    assert check(gate_input(A, "act", "fetch", narrowing=forced), R) == Allow("dispatch")


def test_layer_4_plan_mode_narrowing_stops_saves() -> None:
    plan = StopPolicyNarrowing(frozenset({"fetch"}), by="plan")
    r = _refuse(check(gate_input(A, "gather", "save_artifact", narrowing=plan), R))
    assert (r.code, r.layer, r.detail) == ("narrowed", 4, ("plan",))


def test_layer_5_schema_refusal_names_every_failing_path() -> None:
    r = _refuse(check(gate_input(A, "act", "send", {"extra": 1}), R))
    assert (r.code, r.layer) == ("schema", 5)
    assert r.detail == ("/document", "/extra", "/recipient_id")


def test_layer_6_argument_rule_names_the_rule() -> None:
    args = {"path": ".git/hooks/pre-commit", "text": "x"}
    r = _refuse(check(gate_input(A, "act", "write_file", args), R))
    assert (r.code, r.layer, r.detail) == ("argument_rule", 6, (NO_GIT.label,))
    assert NO_GIT.reason in r.message


def _ceiling_with(*rules: ArgumentRule) -> Permissions:
    return ceiling_of(A, rules=(SEND_TO_LISTED, *rules))


def test_layer_6_a_case_folded_protected_path_is_still_refused() -> None:
    args = {"path": ".GIT/config", "text": "x"}
    r = _refuse(check(gate_input(A, "act", "write_file", args), R))
    assert (r.code, r.layer, r.detail) == ("argument_rule", 6, (NO_GIT.label,))


@pytest.mark.parametrize(
    ("root", "path"),
    [(".git/", ".git"), ("./.git", ".git/config"), (".git//", ".git/hooks/x")],
)
def test_a_path_root_not_in_normal_form_is_refused_at_load(root: str, path: str) -> None:
    rule = replace(NO_GIT, value=root)
    with pytest.raises(RuleInvalid) as err:
        _ceiling_with(rule)
    assert err.value.code == "rule_root_invalid"
    # and a rule built past the loader still compares the root in normal form
    g = gate_input(A, "act", "write_file", {"path": path, "text": "x"})
    unchecked = object.__new__(Permissions)
    object.__setattr__(unchecked, "tools", g.ceiling.tools)
    object.__setattr__(unchecked, "rules", (SEND_TO_LISTED, rule))
    r = _refuse(check(replace(g, ceiling=unchecked), R))
    assert (r.code, r.layer) == ("argument_rule", 6)


@pytest.mark.parametrize("root", ["/abs/.git", "../.git", "", "a\\b"])
def test_an_absolute_or_escaping_root_is_refused_at_load(root: str) -> None:
    with pytest.raises(RuleInvalid) as err:
        _ceiling_with(replace(NO_GIT, value=root))
    assert err.value.code == "rule_root_invalid"


def test_a_recipient_rule_must_use_id_in_source() -> None:
    with pytest.raises(RuleInvalid) as err:
        _ceiling_with(replace(NO_GIT, role="recipient"))
    assert err.value.code == "rule_role_invalid"


def test_layer_6_an_id_in_source_rule_on_another_argument_stays_an_argument_rule() -> None:
    on_document = ArgumentRule("send", "/document", "id_in_source", "docs", "listed documents")
    g = gate_input(A, "act", "send", ceiling=_ceiling_with(on_document))
    r = _refuse(check(g, R))
    assert (r.code, r.layer, r.detail) == ("argument_rule", 6, (on_document.label,))


def test_layer_7_recipient_must_be_listed_in_the_source() -> None:
    args = {"document": "doc:1@v1", "recipient_id": "outside@example.com"}
    r = _refuse(check(gate_input(A, "act", "send", args), R))
    assert (r.code, r.layer, r.detail) == ("recipient_not_listed", 7, (SEND_TO_LISTED.label,))


def test_layer_7_recipient_source_missing_fails_closed() -> None:
    r = _refuse(check(gate_input(A, "act", "send", sources={}), R))
    assert r.code == "recipient_not_listed"


@pytest.mark.parametrize(
    "sandbox",
    [
        None,
        Availability("refuse", "none", available=True),
        Availability("bwrap", "os-jail", available=True),
        Availability("docker", "container", available=False, reason="daemon down"),
    ],
)
def test_layer_7_an_exec_tool_needs_a_strong_enough_sandbox(sandbox: Availability | None) -> None:
    r = _refuse(check(gate_input(A, "build", "run", sandbox=sandbox), R))
    assert (r.code, r.layer) == ("sandbox_unavailable", 7)


def test_layer_order_first_refusing_layer_wins() -> None:
    spent = MeterReading(frozenset({"tool_calls"}))
    assert _refuse(check(gate_input(A, "gather", "nope", meter=spent), R)).layer == 0
    assert _refuse(check(gate_input(A, "gather", "send", {"bad": 1}), R)).layer == 3


class _Raising(ToolRegistry):
    def check_arguments(self, name: str, args: Mapping[str, Any]) -> Any:
        raise RuntimeError("helper broke")


def test_any_exception_is_gate_error_and_never_escapes() -> None:
    broken = _Raising(A, {}, ())
    r = _refuse(check(gate_input(A, "idle", "finish"), broken))
    assert (r.code, r.layer, r.counted) == ("gate_error", 5, True)
    assert r.detail == ("RuntimeError",)


def test_permitted_is_layers_1_to_4_without_arguments() -> None:
    g = gate_input(A, "gather", "send")
    r = permitted(g.call.name, g.agent, g.task, g.ceiling, g.narrowing, R)
    assert r is not None and r.code == "not_in_task"
    assert permitted("fetch", g.agent, g.task, g.ceiling, g.narrowing, R) is None


def test_check_is_pure_same_input_same_answer() -> None:
    g = gate_input(A, "act", "write_file", {"path": ".git/x", "text": ""})
    assert check(g, R) == check(g, R)
