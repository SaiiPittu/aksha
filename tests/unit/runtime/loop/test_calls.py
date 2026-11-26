"""The recorded value of a call (ADR-P10 D7): the capped view, and a result's flags kept."""

from __future__ import annotations

from aksha.runtime.context.reentry import INSTRUCTION_FLAG
from aksha.runtime.loop.calls import _shown
from aksha.runtime.tools.results import VIEW_FIELD
from aksha.runtime.tools.types import Err, Ok, ToolOutcome


def test_a_flagged_result_keeps_its_flag_on_the_recorded_value() -> None:
    # A recall of a benign record whose cited source held instruction-like text: the
    # rendered view has no instruction-like text, so only the flag can taint the session.
    data = {"hits": [{"text": "Approvals for EU vendors."}], "flags": [INSTRUCTION_FLAG]}
    value = _shown(ToolOutcome(Ok(data)), 4000).value
    assert value is not None and VIEW_FIELD in value
    assert INSTRUCTION_FLAG in (value.get("flags") or ())  # what re-entry's taint check reads


def test_an_unflagged_or_failed_result_records_no_flag() -> None:
    plain = _shown(ToolOutcome(Ok({"hits": []})), 4000).value
    other = _shown(ToolOutcome(Ok({"flags": ["something_else"]})), 4000).value
    failed = _shown(ToolOutcome(Err("nope", "refused")), 4000).value
    assert plain is not None and "flags" not in plain
    assert other is not None and "flags" not in other
    assert failed is not None and "flags" not in failed
