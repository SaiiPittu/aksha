"""Which tool results go stale (decision 32) is the invalidation view's answer (ADR-P05 D4, S1, S3).

Masking hides, and the change notice announces, only a read the invalidation view reads
as ``moved`` or ``gone`` for the reader's run: a sibling run's newer version (the UK and EU
case) and an equal rebuild (early cutoff) are not stale; a withdrawn writer's version is.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.runtime.artifacts.types import ArtifactRef, InputRef, WriterId
from aksha.runtime.context.masking import stale_reads
from aksha.runtime.context.types import StaleRead, ToolPair
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.invalidation.view import invalidation_view, withdrawn_writers
from tests.support.invalidation import Bench, record

WRITER = WriterId("p", "write", "N")
NOTE_V1 = ArtifactRef.parse("p.note/N@v1").as_input()


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    b = Bench(tmp_path)
    b.start("R-0001")  # the EU request
    b.save("R-0001", "p.note/N", {"region": "eu"}, writer=WRITER, instance="write[N]")
    yield b
    b.close()


def pair(*reads: InputRef) -> ToolPair:
    """A recorded tool result of the reader ``read`` in R-0001 that read ``reads``."""
    return ToolPair(
        step="R-0001/read/t01/c01", turn=1, position=1, call_id="c1", tool="lookup",
        arguments={}, outcome="ok", reads=reads,
    )  # fmt: skip


def stale(b: Bench) -> tuple[StaleRead, ...]:
    return stale_reads(b.view(), "R-0001", "read", pair(NOTE_V1))


def test_a_sibling_runs_newer_version_is_not_stale(bench: Bench) -> None:
    """S1: an unrelated request of the same agent (the UK one) moves no EU reader."""
    bench.start("R-0002")
    bench.save("R-0002", "p.note/N", {"region": "uk"}, writer=WRITER, instance="write[N]")
    assert stale(bench) == ()


def test_an_equal_rebuild_is_not_stale(bench: Bench) -> None:
    """Early cutoff: a newer version with an equal canonical hash changes nothing."""
    bench.save(
        "R-0001",
        "p.note/N",
        {"region": "eu"},
        writer=WRITER,
        instance="again[N]",
        reads=[record("W-2")],
    )  # other reads, equal content: a new, equal version
    latest = bench.latest("p.note/N")
    assert latest is not None and latest.ref.version == 2
    assert stale(bench) == ()


def test_a_changed_version_of_the_readers_line_is_stale(bench: Bench) -> None:
    bench.save("R-0001", "p.note/N", {"region": "eu-west"}, writer=WRITER, instance="again[N]")
    (found,) = stale(bench)
    assert found == StaleRead(NOTE_V1, ArtifactRef.parse("p.note/N@v2"))
    assert found.slot_id() == "p.note/N"


def test_a_withdrawn_writers_version_is_gone(bench: Bench) -> None:
    """S3, read from the log's own ``task.withdrawn`` (no caller passes the set)."""
    payload = {"task": "write", "key": "N", "reason": "key_withdrawn"}
    withdrawn = NewEvent("task.withdrawn", payload, run="R-0001", instance="write[N]")
    bench.store.append(bench.owner, "U3", (withdrawn,))
    assert withdrawn_writers(bench.view()) == {WRITER}
    assert stale(bench) == (StaleRead(NOTE_V1, None),)
    assert invalidation_view(bench.view()).withdrawn == {WRITER}


def test_a_writer_entered_again_is_no_longer_withdrawn(bench: Bench) -> None:
    payload = {"task": "write", "key": "N", "reason": "key_withdrawn"}
    bench.store.append(bench.owner, "U3", (
        NewEvent("task.withdrawn", payload, run="R-0001", instance="write[N]"),
    ))  # fmt: skip
    entered = {"cause": "scheduler", "task": "write", "key": "N", "writer": WRITER.as_json()}
    bench.store.append(bench.owner, "U3", (
        NewEvent("task.entered", entered, run="R-0001", instance="write[N]"),
    ))  # fmt: skip
    assert withdrawn_writers(bench.view()) == frozenset()
    assert stale(bench) == ()
