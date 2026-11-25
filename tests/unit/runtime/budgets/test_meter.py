"""Budget accounting (ADR-P14 D1, D2): ``meter`` is a reducer over recorded facts."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from aksha.runtime.budgets.meter import BUDGET_ACCOUNTING, Meter, in_scope, meter
from aksha.runtime.budgets.types import Budget
from aksha.runtime.policy.types import MeterReading
from aksha.runtime.projections.cache import ProjectionCache
from aksha.runtime.projections.project import project, rebuild
from tests.support.budgets import RUN, Log


def test_tool_calls_count_counted_starts_and_counted_refusals() -> None:
    log = Log()
    log.started()
    log.tool("s1")
    log.tool("s2", counted=False)
    log.refused("s3")
    log.refused("s4", counted=False, code="budget_exhausted")
    assert meter(log.view(), RUN).used["tool_calls"] == 2


def test_budget_exhausted_not_counted() -> None:
    log = Log()
    log.started({"tool_calls": 1})
    log.tool("s1")
    before = meter(log.view(), RUN)
    log.refused("s2", counted=False, code="budget_exhausted")
    after = meter(log.view(), RUN)
    assert before.used == after.used and after.exhausted() == ("tool_calls",)


def test_a_retry_adds_attempts_never_calls() -> None:
    log = Log()
    log.started()
    log.tool("s1")
    log.failed("s1", 1, duration=10, backoff=500)
    log.failed("s1", 2, duration=10, backoff=1_000)
    log.completed("s1", duration=10)
    m = meter(log.view(), RUN)
    assert m.used["tool_calls"] == 1
    assert m.used["active_ms"] == 10 + 500 + 10 + 1_000 + 10


def test_model_turns_come_from_continue_records_and_a_resume_adds_none() -> None:
    log = Log()
    log.started()
    log.decided("continue", turn=1)
    log.resumed()
    log.decided("continue", turn=1)  # the same turn authorised again
    log.decided("continue", turn=2)
    log.decided("hold")
    assert meter(log.view(), RUN).used["model_turns"] == 2


def test_tokens_and_money_are_summed_from_model_usage() -> None:
    log = Log()
    log.started({"tokens": 100, "cost_micro_usd": 1_000})
    log.completed("m1", kind="model", usage={"input_tokens": 40, "output_tokens": 20})
    log.completed("m2", kind="model", usage={"input_tokens": 30, "cost_micro_usd": 900})
    m = meter(log.view(), RUN)
    assert (m.used["tokens"], m.used["cost_micro_usd"]) == (90, 900)
    assert m.exhausted() == () and m.remaining("tokens") == 10


def test_allowed_is_the_recorded_budget_plus_every_grant() -> None:
    log = Log()
    log.started({"model_turns": 2, "tool_calls": 3, "active_ms": 100})
    log.granted({"tool_calls": 5}, "I-0001")
    log.granted({"tool_calls": 2, "model_turns": 1}, "I-0002")
    m = meter(log.view(), RUN)
    assert dict(m.allowed) == {"model_turns": 3, "tool_calls": 10, "active_ms": 100}


def test_a_grant_never_caps_an_uncapped_dimension() -> None:
    log = Log()
    log.started()
    log.granted({"tokens": 50})
    assert "tokens" not in meter(log.view(), RUN).allowed
    assert meter(log.view(), RUN).remaining("tokens") is None


def test_the_default_applies_when_the_run_recorded_no_budget() -> None:
    log = Log()
    log.started()
    assert meter(log.view(), RUN).allowed["tool_calls"] == 40
    assert meter(log.view(), RUN, default=Budget(tool_calls=7)).allowed["tool_calls"] == 7
    narrowed = Budget(tool_calls=40).narrow(Budget(tool_calls=2))
    assert meter(log.view(), RUN, budget=narrowed).allowed["tool_calls"] == 2


def test_exhausted_at_the_limit_and_remaining_never_negative() -> None:
    log = Log()
    log.started({"tool_calls": 2, "model_turns": 9})
    for n in range(3):
        log.tool(f"s{n}")
    m = meter(log.view(), RUN)
    assert m.exhausted() == ("tool_calls",)
    assert m.remaining("tool_calls") == 0 and m.remaining("model_turns") == 9
    assert m.reading() == MeterReading(frozenset({"tool_calls"}))
    assert Meter({"tool_calls": 1, "model_turns": 0}, {"tool_calls": 2}).exhausted() == ()


def test_another_runs_facts_are_not_counted() -> None:
    log = Log()
    log.started()
    log.started(run="R-0002")
    log.add("step.started", {"kind": "tool", "counted": True}, run="R-0002", step="x")
    log.add("budget.granted", {"interrupt_id": "I", "by": "p", "extra": {"tool_calls": 9}},
            run="R-0002")  # fmt: skip
    m = meter(log.view(), RUN)
    assert m.used["tool_calls"] == 0 and m.allowed["tool_calls"] == 40


@pytest.mark.parametrize(
    ("instance", "scope", "inside"),
    [
        ("compare", None, True),
        (None, None, True),
        (None, "compare", False),
        ("compare", "compare", True),
        ("compare[C1]", "compare", True),
        ("compare[C1]/check", "compare[C1]", True),
        ("compare/sub", "compare", True),
        ("comparex", "compare", False),
        ("scope", "compare", False),
    ],
)
def test_in_scope(instance: str | None, scope: str | None, inside: bool) -> None:
    assert in_scope(instance, scope) is inside


def test_a_scope_counts_only_its_subtree_but_keeps_the_run_allowance() -> None:
    log = Log()
    log.started({"tool_calls": 5})
    log.tool("s1", instance="compare[C1]")
    log.tool("s2", instance="scope")
    log.granted({"tool_calls": 1})
    m = meter(log.view(), RUN, scope="compare")
    assert m.used["tool_calls"] == 1 and m.allowed["tool_calls"] == 6


def test_meter_equals_full_rebuild() -> None:
    log = Log()
    log.started({"tool_calls": 4})
    steps: list[Callable[[], object]] = [
        lambda: log.tool("s1"),
        lambda: log.failed("s1", 1, duration=5, backoff=500),
        lambda: log.completed("s1", duration=7),
        lambda: log.decided("continue", turn=1),
        lambda: log.refused("s2"),
        lambda: log.granted({"tool_calls": 3}),
        lambda: log.completed("m", kind="model", usage={"output_tokens": 4}),
    ]
    cache = ProjectionCache()
    for add in steps:
        add()
        view = log.view()
        cached = project(BUDGET_ACCOUNTING, view, (RUN, None), cache=cache, incremental=True)
        assert cached == rebuild(BUDGET_ACCOUNTING, view, (RUN, None))
        assert meter(view, RUN, cache=cache) == meter(view, RUN)
