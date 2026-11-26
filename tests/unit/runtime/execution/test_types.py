from typing import get_args

import pytest

from aksha.runtime.budgets.types import Backoff
from aksha.runtime.execution.errors import StepIdInvalid
from aksha.runtime.execution.types import (
    STEP_SEMANTICS,
    Deadline,
    StepId,
    StepOutput,
    StepPolicy,
    StepSemantics,
    is_reserved_segment,
)


def test_step_semantics_are_the_three_adr_names() -> None:
    names = {"at_least_once", "reconcile", "at_most_once"}
    assert set(get_args(StepSemantics)) == names
    assert names == STEP_SEMANTICS


@pytest.mark.parametrize(
    ("step", "text"),
    [
        (StepId("R-0002"), "R-0002"),
        (StepId("R-0002", "scope"), "R-0002/scope"),
        (StepId("R-0002", "compare[C1]", turn=3), "R-0002/compare[C1]/t03"),
        (StepId("R-0002", "compare[C1]", turn=3, call=2), "R-0002/compare[C1]/t03/c02"),
        (StepId("R-0002", "compare[C1]", 3, 2, "d01"), "R-0002/compare[C1]/t03/c02/d01"),
        (StepId("R-0002", "compare[C1]", 3, 2, "derived"), "R-0002/compare[C1]/t03/c02/derived"),
        (StepId("R-0003", "prepare[C1]/disclosure", check=1), "R-0003/prepare[C1]/disclosure/k01"),
        (StepId("R-0002", "a", turn=104, call=7), "R-0002/a/t104/c07"),
    ],
)
def test_step_id_round_trip(step: StepId, text: str) -> None:
    assert str(step) == text
    assert StepId.parse(text) == step


def test_step_id_is_computed_from_structure_only() -> None:
    a = StepId("R-0002", "compare[C1]", turn=3, call=2)
    b = StepId.parse("R-0002/compare[C1]/t03/c02")
    assert a == b
    assert hash(a) == hash(b)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "R-12",  # four digits at least
        "X-0001/a",
        "R-0001/",  # empty segment
        "R-0001/a//t01",
        "R-0001/t01",  # a turn needs an instance
        "R-0001/a/c01",  # a call needs a turn
        "R-0001/a/t01/d01",  # a sub needs a call
        "R-0001/a/t00",  # counted from 1
        "R-0001/a/t1",  # two digits at least
        "R-0001/a/t01/c01/x01",
        "R-0001/a/t01/k01",
        "R-0001/a/t01/c01/d01/extra",
        "R-0001/Compare",  # Task names are lower case
        "R-0001/a[]",
        "R-0001/a[C/1]",
        "R-0001/w043",  # walk steps belong to the stop policy, not built
        "R-0001/derived",
    ],
)
def test_step_id_parse_refuses_what_the_grammar_does_not_make(text: str) -> None:
    with pytest.raises(StepIdInvalid) as e:
        StepId.parse(text)
    assert e.value.code == "step_id_invalid"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"run": "R-1"},
        {"run": "R-0001", "turn": 1},
        {"run": "R-0001", "instance": "a", "call": 1},
        {"run": "R-0001", "instance": "a", "turn": 0},
        {"run": "R-0001", "instance": "a", "turn": 1, "check": 1},
        {"run": "R-0001", "instance": "a", "turn": 1, "call": 1, "sub": "x"},
        {"run": "R-0001", "instance": "t03"},
        {"run": "R-0001", "instance": "a/end"},
    ],
)
def test_step_id_refuses_an_invalid_value(kwargs: dict[str, object]) -> None:
    with pytest.raises(StepIdInvalid):
        StepId(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "reserved"),
    [
        ("t03", True),
        ("c1", True),
        ("k01", True),
        ("w043", True),
        ("x2", True),
        ("d01", True),
        ("end", True),
        ("handoff", True),
        ("derived", True),
        ("compact", True),
        ("compare", False),
        ("t", False),
        ("tool", False),
        ("d1x", False),
    ],
)
def test_reserved_segments(name: str, reserved: bool) -> None:
    assert is_reserved_segment(name) is reserved


def test_step_id_compact_sub_step_of_a_turn() -> None:
    step = StepId("R-0001", "edit[a]", turn=12, sub="compact")
    assert str(step) == "R-0001/edit[a]/t12/compact"
    assert StepId.parse(str(step)) == step
    assert StepId("R-0001", "edit[a]", turn=12).compaction() == step
    with pytest.raises(StepIdInvalid):
        StepId("R-0001", "edit[a]", sub="compact")
    with pytest.raises(StepIdInvalid):
        StepId("R-0001", "edit[a]", turn=1, call=1, sub="compact")
    with pytest.raises(StepIdInvalid):
        StepId.parse("R-0001/edit[a]/t12/compact/c01")


def test_step_id_steps_of_a_turn() -> None:
    turn = StepId("R-0001", "a", turn=2)
    assert turn.with_call(1) == StepId("R-0001", "a", turn=2, call=1)
    assert str(StepId("R-0001", "a").with_turn(1)) == "R-0001/a/t01"


def test_backoff_delay_doubles_to_the_cap() -> None:
    policy = StepPolicy(backoff=Backoff(base_ms=500, factor=2, cap_ms=1_500))
    assert [policy.delay_ms(a) for a in (1, 2, 3, 4)] == [500, 1_000, 1_500, 1_500]


def test_step_policy_refuses_unknown_semantics() -> None:
    with pytest.raises(ValueError, match="semantics"):
        StepPolicy(semantics="rerun")  # type: ignore[arg-type]


def test_deadline_counts_down_on_the_monotonic_clock() -> None:
    now = [1_000]
    d = Deadline(at_ms=1_250, mono_ms=lambda: now[0])
    assert d.remaining_ms() == 250
    assert not d.expired()
    now[0] = 1_300
    assert d.remaining_ms() == 0
    assert d.expired()


def test_step_output_defaults_to_ok() -> None:
    out = StepOutput({"n": 1})
    assert out.outcome == "ok"
    assert out.code is None
    with pytest.raises(ValueError, match="code"):
        StepOutput({"n": 1}, outcome="error")
