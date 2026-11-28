"""A limit ends an instance as a finish would when its saved work passes its check (ADR-P16 S5).

The stop-policy tests drive the policy alone over a one-Task tree; the last two run the
loop's ``coder`` agent end to end, so the facts come from the loop's own commits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.stop_policy.core import TaskPolicy
from aksha.runtime.stop_policy.types import StopCandidate, Verdict
from aksha.runtime.tasks.status import statuses
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import Task
from tests.support.budgets import RUN
from tests.support.loop import Bench, Recorder, call, tools_reply
from tests.support.stop import decide, view_of
from tests.support.tasks import AGENT, TaskLog, has, ref


def _log() -> TaskLog:
    log = TaskLog()
    log.started()
    return log


def test_the_turn_limit_after_a_passing_save_ends_the_instance_done() -> None:
    tree = compile_tree(AGENT, (Task("only", writes=("x",), done=has("x"), turn_limit=2),))
    log = _log()
    decide(log, tree, StopCandidate(RUN, "next"))
    decide(log, tree, StopCandidate(RUN, "tools", ref("only"), after_turn=1))
    log.save("x", None, {"ok": True}, instance="only")  # the last allowed turn saved it
    d = decide(log, tree, StopCandidate(RUN, "tools", ref("only"), after_turn=2))
    assert d.limit == "turn_limit"
    assert d.counts == {"turns": 2, "allowed": 2}
    assert d.pops == (("only", "done"),)
    evaluated = [f for f in d.facts if f.type == "completion_check.evaluated"]
    assert [(f.payload["on"], f.payload["ok"]) for f in evaluated] == [("limit", True)]
    assert statuses(log.view(), tree, RUN)["only"] == "done"
    assert d.verdict.kind == "stop" and d.verdict.outcome == "complete"


def test_the_turn_limit_with_a_failing_check_fails_and_records_why() -> None:
    tree = compile_tree(AGENT, (Task("only", writes=("x",), done=has("x"), turn_limit=2),))
    log = _log()
    decide(log, tree, StopCandidate(RUN, "next"))
    decide(log, tree, StopCandidate(RUN, "tools", ref("only"), after_turn=1))
    d = decide(log, tree, StopCandidate(RUN, "tools", ref("only"), after_turn=2))
    assert d.limit == "turn_limit"
    assert d.pops == (("only", "fail"),)
    evaluated = [f for f in d.facts if f.type == "completion_check.evaluated"]
    assert [(f.payload["on"], f.payload["ok"]) for f in evaluated] == [("limit", False)]
    assert evaluated[0].payload["message"] == "save x first"


def _only() -> tuple[Any, TaskLog]:
    """``only`` (turn limit 2) entered and past its first turn: the next candidate bites."""
    tree = compile_tree(AGENT, (Task("only", writes=("x",), done=has("x"), turn_limit=2),))
    log = _log()
    decide(log, tree, StopCandidate(RUN, "next"))
    decide(log, tree, StopCandidate(RUN, "tools", ref("only"), after_turn=1))
    return tree, log


def _on(d: Any, on: str) -> list[Any]:
    return [f for f in d.facts if f.type == "completion_check.evaluated" and f.payload["on"] == on]


@pytest.mark.parametrize(
    "candidate",
    [
        StopCandidate(RUN, "malformed", ref("only"), after_turn=2),
        StopCandidate(RUN, "next"),  # the scheduler picks the still-active leaf again
    ],
    ids=["malformed", "next"],
)
def test_every_after_turn_candidate_checks_the_saved_work_at_the_limit(
    candidate: StopCandidate,
) -> None:
    tree, log = _only()
    log.save("x", None, {"ok": True}, instance="only")
    d = decide(log, tree, candidate)
    assert d.limit == "turn_limit"
    assert d.pops == (("only", "done"),)
    assert [f.payload["ok"] for f in _on(d, "limit")] == [True]


def test_a_refused_finish_at_the_limit_fails_with_one_evaluation_on_finish() -> None:
    tree, log = _only()
    d = decide(log, tree, StopCandidate(RUN, "finish", ref("only"), after_turn=2))
    assert d.limit == "turn_limit"
    assert d.pops == (("only", "fail"),)
    evaluated = [f for f in d.facts if f.type == "completion_check.evaluated"]
    assert [(f.payload["on"], f.payload["ok"]) for f in evaluated] == [("finish", False)]


def _queue(log: TaskLog, lane: str) -> None:
    log.add("input.queued", {"input_id": "IN-0001", "lane": lane, "text": "one more thing",
                             "instance": "only", "by": "person",
                             "command_id": "c-IN-0001"})  # fmt: skip


@pytest.mark.parametrize("lane", ["follow_up", "steer"])
def test_a_pending_follow_up_or_steer_keeps_the_fail_at_the_limit(lane: str) -> None:
    """A finish in this state would be vetoed by the person's input, so the limit is no DONE."""
    tree, log = _only()
    log.save("x", None, {"ok": True}, instance="only")
    _queue(log, lane)
    d = decide(log, tree, StopCandidate(RUN, "tools", ref("only"), after_turn=2))
    assert d.limit == "turn_limit"
    assert d.pops == (("only", "fail"),)
    assert _on(d, "limit") == []


def test_a_parent_at_the_limit_fails_without_evaluating_its_check() -> None:
    child = Task("edit", writes=("x",), done=has("x"))
    tree = compile_tree(AGENT, (Task("lead", children=(child,), brief="Steer.", done=has("x")),))
    log = _log()
    log.save("x", None, {"ok": True})
    policy = TaskPolicy(ref("lead"))
    c = StopCandidate(RUN, "tools", ref("lead"), after_turn=1)
    counts = (("turns", 1), ("allowed", 1))
    v = policy._at_limit(view_of(log, tree), c, Verdict("continue"), "limit", "turn_limit", counts)
    assert v.kind == "fail" and v.facts == ()


SAVE = call("save_artifact", kind="note", content={"done": True})


def test_a_task_that_saves_passing_work_on_its_last_turn_ends_done(tmp_path: Path) -> None:
    """The model saves on turn 2 of 2 and never asks to finish: the run still completes."""
    read = tools_reply(call("read_file", path="a.py"))
    b = Bench(tmp_path / "s.sqlite", Recorder([read, tools_reply(SAVE)]), turn_limit=2)
    b.start()
    assert b.drive().outcome == "complete"
    (hit,) = [d for d in b.decisions() if d.payload.get("limit") == "turn_limit"]
    assert dict(hit.payload["counts"]) == {"turns": 2, "allowed": 2}
    assert [tuple(p) for p in hit.payload["pops"]] == [("edit", "done")]
    on_limit = [e for e in b.events("completion_check.evaluated") if e.payload["on"] == "limit"]
    assert [(e.payload["ok"], e.instance) for e in on_limit] == [(True, "edit")]
    b.close()


def test_a_task_whose_saved_work_fails_its_check_still_fails_at_the_limit(
    tmp_path: Path,
) -> None:
    """Two turns of reading and no note: the limit fails ``edit`` with the check's message."""
    reads = [tools_reply(call("read_file", n, path="a.py")) for n in (1, 2)]
    b = Bench(tmp_path / "s.sqlite", Recorder(reads), turn_limit=2)
    b.start()
    assert b.drive().outcome == "blocked"
    (hit,) = [d for d in b.decisions() if d.payload.get("limit") == "turn_limit"]
    assert [tuple(p) for p in hit.payload["pops"]] == [("edit", "fail")]
    on_limit = [e for e in b.events("completion_check.evaluated") if e.payload["on"] == "limit"]
    assert [(e.payload["ok"], e.payload["message"]) for e in on_limit] == [
        (False, "save a note first")
    ]
    b.close()
