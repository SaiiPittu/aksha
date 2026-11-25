from typing import get_args

from aksha.runtime.execution.types import STEP_SEMANTICS, StepSemantics


def test_step_semantics_are_the_three_adr_names() -> None:
    names = {"at_least_once", "reconcile", "at_most_once"}
    assert set(get_args(StepSemantics)) == names
    assert names == STEP_SEMANTICS
