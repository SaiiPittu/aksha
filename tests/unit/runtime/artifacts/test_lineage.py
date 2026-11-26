"""Lineage (ADR-P04 D6): reads are recorded facts, and the graph is drawn from them only."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.runtime.artifacts.lineage import read_recorded, reads_to_record, recorded_reads
from aksha.runtime.artifacts.types import ArtifactRef, InputRef
from tests.support.artifacts import COMPARE, RUN, World, page


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    yield w
    w.close()


def test_a_read_is_recorded_once_per_instance_and_run(world: World) -> None:
    world.read("gather", page(1), page(1))
    world.read("gather", page(1), page(2))
    world.read("gather", page(3), run="R-0002")
    view = world.view()
    assert recorded_reads(view, RUN, "gather") == {page(1), page(2)}
    assert recorded_reads(view, "R-0002", "gather") == {page(3)}
    assert recorded_reads(view, RUN, "compare") == frozenset()
    assert reads_to_record(view, [page(1)], run=RUN, instance="gather") == ()


def test_a_read_names_its_run_and_instance() -> None:
    event = read_recorded(page(1), run=RUN, instance="gather", via="context")
    assert (event.type, event.run, event.instance, event.payload["via"]) == (
        "read.recorded", RUN, "gather", "context",
    )  # fmt: skip
    with pytest.raises(ValueError):
        read_recorded(page(1), run=RUN, instance="")


def test_lineage_up_and_down(world: World) -> None:
    world.read("gather", page(1))
    world.save("line", "n1", {"text": "a"})
    n1 = InputRef("artifact", "notes.line/n1", "v1")
    world.read("compare", n1, page(2))
    world.save("summary", None, {"s": "x"}, writer=COMPARE, instance="compare")
    view = world.view()
    summary, line = ArtifactRef("notes.summary", None, 1), ArtifactRef("notes.line", "n1", 1)
    up = world.artifacts.lineage(view, summary)
    assert up.nodes == {summary.as_input(), n1, page(1), page(2)}
    assert up.edges == {(n1, summary.as_input()), (page(2), summary.as_input()), (page(1), n1)}
    assert world.artifacts.lineage(view, summary, depth=1).nodes == {
        summary.as_input(),
        n1,
        page(2),
    }
    down = world.artifacts.lineage(view, line, direction="down")
    assert down.nodes == {n1, summary.as_input()} and down.edges == {(n1, summary.as_input())}
