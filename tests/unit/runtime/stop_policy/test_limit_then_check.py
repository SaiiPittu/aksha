"""A limit ends an instance as a finish would when its saved work passes its check (ADR-P16 S5).

The first two tests drive the stop policy alone over a one-Task tree; the last two run the
loop's ``coder`` agent end to end, so the facts come from the loop's own commits.
"""

from __future__ import annotations

from pathlib import Path

from aksha.runtime.stop_policy.types import StopCandidate
from aksha.runtime.tasks.status import statuses
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import Task
from tests.support.budgets import RUN
from tests.support.loop import Bench, Recorder, call, tools_reply
from tests.support.stop import decide
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
