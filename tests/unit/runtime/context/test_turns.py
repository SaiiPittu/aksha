from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.context.turns import NO_RESULT, as_of, instance_turns
from aksha.runtime.execution.types import StepId
from aksha.runtime.tasks.types import InstanceRef
from tests.support.context import RUN, Log, result_view

EDIT = InstanceRef(RUN, "edit", "parser")


@pytest.fixture
def log(tmp_path: Path) -> Iterator[Log]:
    log = Log(tmp_path / "aksha.sqlite")
    log.start()
    yield log
    log.close()


def test_turns_pair_each_call_with_its_recorded_result(log: Log) -> None:
    log.model_turn(EDIT, 1, (("read_file", {"path": "a.py"}), ("run_tests", {})))
    read = InputRef("record", "files/a.py", "1")
    log.tool_result(EDIT, 1, 1, "read_file", result_view("def a(): ..."), reads=(read,))
    log.model_turn(EDIT, 2, text="done")
    turns = instance_turns(log.view(), RUN, EDIT.instance)
    assert [t.number for t in turns] == [1, 2]
    first, second = turns[0].pairs
    assert (first.tool, first.call_id, first.arguments, first.reads) == (
        "read_file",
        "call-1-1",
        {"path": "a.py"},
        (read,),
    )
    assert first.view is not None and "def a()" in first.text
    assert (second.outcome, second.view) == (NO_RESULT, None)  # the call stays paired
    assert NO_RESULT in second.text
    assert turns[1].text == "done" and turns[1].pairs == ()


def test_turns_are_the_instances_own(log: Log) -> None:
    other = InstanceRef(RUN, "edit", "lexer")
    log.model_turn(EDIT, 1, text="mine")
    log.model_turn(other, 1, text="not mine")
    log.model_turn(InstanceRef("R-0002", "edit", "parser"), 1, text="another run")
    assert [t.text for t in instance_turns(log.view(), RUN, EDIT.instance)] == ["mine"]


def test_as_of_reads_the_log_before_a_step_started(log: Log) -> None:
    log.model_turn(EDIT, 1, text="one")
    t2 = log.model_turn(EDIT, 2, text="two")
    full = log.view()
    before = as_of(full, t2)
    assert [t.number for t in instance_turns(before, RUN, EDIT.instance)] == [1]
    unstarted = StepId(RUN, EDIT.instance, turn=3)
    assert as_of(full, unstarted).seq_read == full.seq_read
