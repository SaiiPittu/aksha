"""Agent loop values (ADR-P06): turn ends and the stop candidates they are."""

from __future__ import annotations

import pytest

from aksha.runtime.loop.types import CANDIDATE_OF, TURN_END_KINDS, TurnEnd, skip_code
from aksha.runtime.stop_policy.types import CANDIDATE_KINDS
from aksha.runtime.tasks.types import InstanceRef


def test_every_turn_end_kind_is_a_stop_candidate_kind() -> None:
    assert set(CANDIDATE_OF) == TURN_END_KINDS
    assert set(CANDIDATE_OF.values()) <= CANDIDATE_KINDS


def test_a_turn_end_is_its_instance_candidate() -> None:
    c = TurnEnd("R-0002", "compare[C1]", 3, "tool_results").candidate()
    assert (c.run, c.kind, c.instance, c.after_turn) == (
        "R-0002", "tools", InstanceRef("R-0002", "compare", "C1"), 3,
    )  # fmt: skip


def test_a_step_mismatch_is_the_run_fault() -> None:
    c = TurnEnd("R-0002", "compare[C1]", 3, "fault", "step_mismatch").candidate()
    assert (c.kind, c.instance, c.reason) == ("fault", None, "step_mismatch")
    other = TurnEnd("R-0002", "compare[C1]", 3, "fault", "model_unavailable").candidate()
    assert other.instance == InstanceRef("R-0002", "compare", "C1")


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="one of the eight"):
        TurnEnd("R-0001", "edit", 1, "done")  # type: ignore[arg-type]


def test_skip_codes_name_the_signal() -> None:
    assert skip_code("approval") == "after_approval"
