"""The artifact store through the step executor: one transaction, replay, and offload.

A save's facts ride on the step's ``StepOutput`` and commit with its ``step.completed`` in
U2 (ADR-P04 D7), so a step that is run again replays its recorded result and writes no
second row. With a blob store, a step value over the 64 KiB cap is stored as a stub and
read back whole (decision 25).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.runtime.artifacts.types import SaveRequest
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.events.errors import PayloadTooLarge
from aksha.runtime.events.stubs import is_blob_stub
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.types import Deadline, StepId, StepOutput, StepPolicy
from aksha.runtime.storage.errors import AlreadyRecorded
from tests.support.artifacts import GATHER, OWNER, RUN, WRITES, World, page
from tests.support.steps import HASH

SID = StepId(RUN, "gather", turn=1, call=1)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    yield w
    w.close()


def _save_step(world: World, calls: list[int]) -> StepOutput:
    calls.append(1)
    view = world.view()
    read = (page(1),)
    from aksha.runtime.artifacts.lineage import reads_to_record

    reads = reads_to_record(view, read, run=RUN, instance="gather", step=str(SID))
    saved = world.artifacts.save(
        view, SaveRequest("line", "n1", {"text": "a"}), writer=GATHER, run=RUN,
        instance="gather", step=str(SID), writes=WRITES, pending_reads=read,
    )  # fmt: skip
    value = {"ref": str(saved.version.ref)}
    return StepOutput(value=value, events=(*reads, *saved.events), artifacts=saved.rows)


def test_a_save_commits_with_its_step_and_a_rerun_replays_it(world: World) -> None:
    calls: list[int] = []
    executor = StepExecutor(world.store, OWNER, world.clock)

    def work(_: Deadline) -> StepOutput:
        return _save_step(world, calls)

    first = executor.step(SID, "tool", "save_artifact", HASH, work, StepPolicy())
    again = executor.step(SID, "tool", "save_artifact", HASH, work, StepPolicy())
    assert first.value == again.value == {"ref": "notes.line/n1@v1"}
    assert again.replayed and calls == [1]
    assert len(world.store.read_artifacts(OWNER)) == 1
    types = [e.type for e in world.store.read(OWNER) if e.step == str(SID)]
    assert types == ["step.started", "read.recorded", "artifact.saved", "step.completed"]
    v = world.artifacts.latest(world.view(), "notes.line", "n1")
    assert v is not None and v.reads == (page(1),) and v.step == str(SID)


def test_version_cannot_be_overwritten(world: World, tmp_path: Path) -> None:
    """A second save of one ref is refused by its dedup key, and the table refuses edits."""
    saved = world.save("line", "n1", {"text": "a"})
    with pytest.raises(AlreadyRecorded):
        world.store.append(OWNER, "U2", saved.events, saved.rows)
    raw = sqlite3.connect(tmp_path / "aksha.sqlite")
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            raw.execute("UPDATE artifacts SET content = '{}'")
        with pytest.raises(sqlite3.IntegrityError):
            row = raw.execute("SELECT * FROM artifacts").fetchone()
            raw.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?)", row)
    finally:
        raw.close()


def test_a_large_step_value_is_offloaded_and_read_back_whole(world: World) -> None:
    big = {"stdout": "a failing test\n" * 6000}
    sid = StepId(RUN, "gather", turn=1, call=2)

    def work(_: Deadline) -> StepOutput:
        return StepOutput(value=big)

    without = StepExecutor(world.store, OWNER, world.clock)
    with pytest.raises(PayloadTooLarge):
        without.step(sid, "tool", "run_tests", HASH, work, StepPolicy())
    executor = StepExecutor(world.store, OWNER, world.clock, blobs=world.blobs)
    result = executor.step(sid, "tool", "run_tests", HASH, work, StepPolicy())
    assert result.value == big
    (done,) = [e for e in world.store.read(OWNER) if e.type == "step.completed"]
    assert is_blob_stub(done.payload["value"]["stdout"])
    replayed = executor.step(sid, "tool", "run_tests", HASH, work, StepPolicy())
    assert replayed.replayed and replayed.value == big


def test_an_offloaded_event_appends_with_its_stub(world: World) -> None:
    big = "x" * 70_000
    event = NewEvent("probe.free", {"blob": big}, run=RUN)
    from aksha.runtime.artifacts.blobs import offload_events

    (moved,) = offload_events(world.store.catalogue, world.blobs, [event])
    (stored,) = world.store.append(OWNER, "U2", [moved])
    assert is_blob_stub(stored.payload["blob"])
