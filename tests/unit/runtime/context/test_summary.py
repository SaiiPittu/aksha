from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from aksha.runtime.context.summary import (
    SUMMARY_INSTRUCTIONS,
    compacted_event,
    summary_request,
    summary_through,
)
from aksha.runtime.context.turns import instance_turns
from aksha.runtime.context.types import (
    CONTEXT_POLICY_VERSION,
    Compaction,
    ContextPolicy,
    MaskingPlan,
    SummaryPolicy,
)
from aksha.runtime.execution.types import StepId
from aksha.runtime.tasks.types import InstanceRef
from tests.support.context import RUN, Log

EDIT = InstanceRef(RUN, "edit", "parser")
POLICY = ContextPolicy(summary=SummaryPolicy(trigger_chars=1_000, keep_recent_pairs=2))
STEP = StepId(RUN, EDIT.instance, turn=6)


def plan(final: int, over: bool = False) -> MaskingPlan:
    return MaskingPlan(EDIT.instance, 6, CONTEXT_POLICY_VERSION, final, final, (), False, over)


@pytest.fixture
def log(tmp_path: Path) -> Iterator[Log]:
    log = Log(tmp_path / "aksha.sqlite")
    log.start()
    for t in range(1, 6):
        log.turn_with_results(EDIT, t, (("run_tests", {"n": t}, f"result {t}"),))
    yield log
    log.close()


def test_summary_covers_whole_turns_older_than_the_kept_pairs(log: Log) -> None:
    turns = instance_turns(log.view(), RUN, EDIT.instance)
    assert summary_through(log.view(), STEP, turns, plan(5_000), POLICY) == 3


def test_no_summary_under_the_trigger_or_without_a_policy(log: Log) -> None:
    turns = instance_turns(log.view(), RUN, EDIT.instance)
    assert summary_through(log.view(), STEP, turns, plan(900), POLICY) is None
    assert summary_through(log.view(), STEP, turns, plan(900, over=True), POLICY) == 3
    no = replace(POLICY, summary=None)
    assert summary_through(log.view(), STEP, turns, plan(5_000), no) is None


def test_no_summary_past_the_per_instance_limit(log: Log) -> None:
    turns = instance_turns(log.view(), RUN, EDIT.instance)
    zero = replace(POLICY, summary=SummaryPolicy(trigger_chars=1_000, max_per_instance=0))
    assert summary_through(log.view(), STEP, turns, plan(5_000), zero) is None


def test_summary_request_and_event() -> None:
    from aksha.runtime.context.types import ToolPair, Turn
    from aksha.runtime.models.types import ToolCall

    pair = ToolPair(f"{RUN}/{EDIT.instance}/t02/c01", 2, 1, "c", "run_tests", {"n": 2}, "ok")
    turns = (
        Turn(1, "s1", "thinking", (), ()),
        Turn(2, "s2", None, (ToolCall("c", "run_tests", {"n": 2}),), (pair,)),
        Turn(3, "s3", "later", (), ()),
    )
    previous = Compaction(1, "turn one summary", "sha256:" + "0" * 64, "x")
    request = summary_request(STEP, turns, 2, POLICY, previous)
    assert request.step_id == f"{RUN}/{EDIT.instance}/t06/compact"
    assert request.alias == "generate.summary"
    assert request.messages[0].text == SUMMARY_INSTRUCTIONS
    body = request.messages[1].text or ""
    assert body.startswith("Earlier summary (turns 1 to 1):\nturn one summary")
    assert "thinking" not in body and "later" not in body and 'run_tests {"n":2}' in body
    event = compacted_event(STEP, 2, "text")
    assert (event.type, event.step, event.payload["through_turn"]) == (
        "context.compacted",
        f"{RUN}/{EDIT.instance}/t06/compact",
        2,
    )
