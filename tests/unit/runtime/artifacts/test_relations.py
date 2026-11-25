"""The relation index and ``components`` (ADR-P04 S2)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.runtime.artifacts.types import RelationRequest
from tests.support.artifacts import World, page


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    yield w
    w.close()


def _same(world: World, a: int, b: int) -> RelationRequest:
    evidence = world.cite("gather", page(a), f"name {a}")
    return RelationRequest("same_as", page(a), page(b), (evidence,))


def test_rows_equal_components(world: World) -> None:
    """Entities, not hits: four pages joined by three relations are two entities and one."""
    world.read("gather", *(page(n) for n in range(1, 6)))
    world.save("merge", "m1", {}, relations=(_same(world, 1, 2), _same(world, 3, 2)))
    world.save("merge", "m2", {}, relations=(_same(world, 4, 5),))
    groups = world.artifacts.components(world.view(), "notes.same_as")
    assert groups == (frozenset({page(1), page(2), page(3)}), frozenset({page(4), page(5)}))


def test_relations_filter_by_kind_end_and_currency(world: World) -> None:
    world.read("gather", page(1), page(2), page(3))
    world.save("merge", "m1", {}, relations=(_same(world, 1, 2),))
    world.save("merge", "m1", {"v": 2}, relations=(_same(world, 2, 3),))
    view = world.view()
    current = world.artifacts.relations(view, kind="notes.same_as")
    assert [(r.src, r.dst) for r in current] == [(page(2), page(3))]
    assert world.artifacts.relations(view, touching=page(1)) == ()
    from aksha.runtime.artifacts.relations import relations

    every = relations(view, kind="notes.same_as", current_only=False)
    assert [r.carrier.version for r in every] == [1, 2]
