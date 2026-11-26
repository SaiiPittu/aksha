from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.blobs.filesystem import FilesystemBlobStore
from aksha.runtime.artifacts.blobs import offload
from aksha.runtime.artifacts.errors import BlobError
from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.context.turns import NO_RESULT, as_of, instance_turns
from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, PAYLOAD_CAP_BYTES, Catalogue
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.types import StepId
from aksha.runtime.loop.classify import recorded_turn_end
from aksha.runtime.models.codec import generation_result_json
from aksha.runtime.models.types import GenerationResult, ToolCall
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


def _offloaded_turn(log: Log, blobs: FilesystemBlobStore, text: str) -> None:
    """A model turn whose reply is over the payload cap, stored as the executor stores it."""
    step = StepId(RUN, EDIT.instance, turn=1)
    env = {"run": RUN, "instance": EDIT.instance, "step": str(step)}
    started = {
        "kind": "model",
        "name": "generate",
        "args_hash": "sha256:" + "0" * 64,
        "counted": True,
    }
    log.store.append("o", "U2a", (NewEvent("step.started", started, **env),))
    reply = GenerationResult(text, (ToolCall("call-1-1", "run_tests", {}),), "tool_calls")
    done = {
        "outcome": "ok", "kind": "model", "name": "generate", "attempts": 1,
        "value": {"result": generation_result_json(reply)},
    }  # fmt: skip
    payload = offload(Catalogue(CORE_EVENT_TYPES), blobs, "step.completed", done)
    assert payload != done  # the reply is a stub now
    log.store.append("o", "U2", (NewEvent("step.completed", payload, **env),))


def test_an_offloaded_reply_reads_as_recorded(log: Log, tmp_path: Path) -> None:
    """Decision 25: a reply moved to the blob store is inflated, not sent as its stub."""
    blobs = FilesystemBlobStore(tmp_path / "blobs")
    text = "x" * (PAYLOAD_CAP_BYTES + 10)
    _offloaded_turn(log, blobs, text)
    (turn,) = instance_turns(log.view(), RUN, EDIT.instance, blobs=blobs)
    assert turn.text == text and [c.name for c in turn.calls] == ["run_tests"]
    with pytest.raises(BlobError) as e:
        instance_turns(log.view(), RUN, EDIT.instance)
    assert e.value.code == "blob_store_required"


def test_an_offloaded_reply_ends_its_turn_as_recorded(log: Log, tmp_path: Path) -> None:
    """Many small calls: the whole call list moves to the blob store, and still counts."""
    blobs = FilesystemBlobStore(tmp_path / "blobs")
    step = StepId(RUN, EDIT.instance, turn=1)
    env = {"run": RUN, "instance": EDIT.instance, "step": str(step)}
    started = {"kind": "model", "name": "generate", "args_hash": "sha256:" + "0" * 64,
               "counted": True}  # fmt: skip
    log.store.append("o", "U2a", (NewEvent("step.started", started, **env),))
    calls = tuple(ToolCall(f"call-1-{n}", "run_tests", {"note": "z" * 800}) for n in range(1, 101))
    done = {
        "outcome": "ok", "kind": "model", "name": "generate", "attempts": 1,
        "value": {"result": generation_result_json(GenerationResult(None, calls, "tool_calls"))},
    }  # fmt: skip
    payload = offload(Catalogue(CORE_EVENT_TYPES), blobs, "step.completed", done)
    assert payload != done
    log.store.append("o", "U2", (NewEvent("step.completed", payload, **env),))
    # The calls have no recorded results yet, so the turn is unfinished, not a text reply.
    assert recorded_turn_end(log.view(), EDIT, 1, blobs=blobs) is None
    for n in range(1, 101):
        log.tool_result(EDIT, 1, n, "run_tests", result_view("ok"))
    end = recorded_turn_end(log.view(), EDIT, 1, blobs=blobs)
    assert end is not None and end.kind not in ("malformed", "text")


def _cited_result(log: Log, turn: int, tool: str, *, read: InputRef | None) -> None:
    """A tool step citing span EX-0001 of ``files/a.py``; it mints the span when it reads."""
    from aksha.runtime.tools.results import view_json

    step = StepId(RUN, EDIT.instance, turn=turn, call=1)
    env = {"run": RUN, "instance": EDIT.instance, "step": str(step)}
    started = {"kind": "tool", "tool": tool, "args_hash": "sha256:" + "1" * 64, "counted": True}
    log.store.append("o", "U2a", (NewEvent("step.started", started, **env),))
    events = []
    if read is not None:
        span = {
            "span_id": "EX-0001", "source": "corpus", "record_id": read.id,
            "version": read.version, "start": 0, "end": 3, "text": "def", "tool": tool,
        }  # fmt: skip
        minted = {"tool": tool, "spans": [span], "shown": ["EX-0001"]}
        events.append(NewEvent("excerpts.minted", minted, **env))
        events.append(NewEvent("read.recorded", {"input": read.as_json(), "via": "tool"}, **env))
    value = {"view": view_json(result_view("def a(): ...")), "spans": ["EX-0001"]}
    done = {"outcome": "ok", "kind": "tool", "tool": tool, "attempts": 1, "value": value}
    events.append(NewEvent("step.completed", done, **env))
    log.store.append("o", "U2", tuple(events))


def test_a_second_read_of_the_same_version_still_names_its_input(log: Log) -> None:
    """A read is recorded once per instance, so the second tool that returned the same
    version records none; its pair still names the input (through the span it cites), so
    the result is found stale when that record moves (decision 32, decision 36)."""
    read = InputRef("record", "files/a.py", "1")
    log.model_turn(EDIT, 1, (("search", {}),))
    _cited_result(log, 1, "search", read=read)
    log.model_turn(EDIT, 2, (("read_file", {"path": "a.py"}),))
    _cited_result(log, 2, "read_file", read=None)
    first, second = (t.pairs[0] for t in instance_turns(log.view(), RUN, EDIT.instance))
    assert first.reads == (read,)
    assert second.reads == (read,)
