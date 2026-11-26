"""The artifact reader a read tool is given (ADR-P09 D3, D5): current versions, as of a view.

A tool that declares ``reads_kinds`` reads the owner's artifacts of those kinds through
``ToolContext.artifacts``: the current version of every slot of a kind, or one version by
ref, each with its freshness in the invalidation view and whether a newer version exists.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.runtime.artifacts.types import WriterId
from aksha.runtime.invalidation.commands import apply_source_change
from aksha.runtime.invalidation.reading import KindNotReadable, ViewArtifactReader
from tests.support.invalidation import OWNER, Bench, FakeSource, record


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    b = Bench(tmp_path)
    yield b
    b.close()


def test_latest_lists_the_current_version_of_every_slot_sorted_by_key(bench: Bench) -> None:
    writer = WriterId("r", "scope", None)
    bench.start("R-0001", agent="r")
    bench.save("R-0001", "r.finding/b", {"n": 1}, writer=writer)
    bench.save("R-0001", "r.finding/a", {"n": 1}, writer=writer)
    bench.start("R-0002", agent="r")
    bench.save("R-0002", "r.finding/b", {"n": 2}, writer=writer)
    reader = ViewArtifactReader(bench.view(), frozenset({"r.finding"}))
    found = reader.latest("r.finding")
    assert [str(s.ref) for s in found] == ["r.finding/a@v1", "r.finding/b@v2"]
    assert found[1].content == {"n": 2}
    assert found[1].writer == writer and found[1].run == "R-0002"
    assert all(s.is_latest and s.freshness == "current" for s in found)


def test_get_names_an_old_version_and_says_it_is_not_the_latest(bench: Bench) -> None:
    writer = WriterId("r", "scope", None)
    bench.start("R-0001", agent="r")
    bench.save("R-0001", "r.finding/a", {"n": 1}, writer=writer)
    bench.save("R-0001", "r.finding/a", {"n": 2}, writer=writer)
    reader = ViewArtifactReader(bench.view(), frozenset({"r.finding"}))
    old = reader.get("r.finding/a@v1")
    assert old is not None and not old.is_latest and old.content == {"n": 1}
    assert reader.get("r.finding/a@v9") is None


def test_freshness_comes_from_the_invalidation_view(bench: Bench) -> None:
    bench.start("R-0001")
    bench.save("R-0001", "p.extract/X", {"v": 1}, reads=[record("K")])
    source = FakeSource()
    source.change(OWNER, {"refs/K": "2"})
    assert apply_source_change(bench.store, OWNER, [source], "cs").accepted
    reader = ViewArtifactReader(bench.view(), frozenset({"p.extract"}))
    (extract,) = reader.latest("p.extract")
    assert extract.freshness == "invalidated" and extract.is_latest


def test_a_kind_the_tool_did_not_declare_is_refused(bench: Bench) -> None:
    reader = ViewArtifactReader(bench.view(), frozenset({"r.finding"}))
    with pytest.raises(KindNotReadable):
        reader.latest("p.note")
    with pytest.raises(KindNotReadable):
        reader.get("p.note/n@v1")
    assert reader.latest("r.finding") == ()
