"""The invalidation view: read states, the four version states, early cutoff, the sibling rule."""

from __future__ import annotations

import random
from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.runtime.artifacts.hashing import entry_hash
from aksha.runtime.artifacts.types import ArtifactRef, InputRef, WriterId
from aksha.runtime.artifacts.versions import all_versions
from aksha.runtime.invalidation.commands import apply_source_change
from aksha.runtime.invalidation.view import invalidation_view, runs_for
from aksha.runtime.projections.view import LogView
from tests.support.invalidation import OWNER, Bench, FakeSource, record, ref


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    b = Bench(tmp_path)
    yield b
    b.close()


def a(text: str) -> InputRef:
    return ref(text).as_input()


def move(b: Bench, *records: str, gone: bool = False) -> None:
    source = FakeSource()
    source.versions[OWNER] = len([e for e in b.events() if e.type == "source.changed"])
    source.change(OWNER, {f"refs/{r}": None if gone else "2" for r in records})
    assert apply_source_change(b.store, OWNER, [source], "cs").accepted


def chain(b: Bench, run: str = "R-0001") -> None:
    b.start(run)
    b.save(run, "p.extract/X", {"v": 1}, reads=[record("K")])
    b.save(run, "p.compare/X", {"rows": []}, reads=[a("p.extract/X@v1")])
    b.save(run, "p.recommend/X", {"go": 1}, reads=[a("p.compare/X@v1")])


def test_a_moved_record_invalidates_its_reader_and_dirties_the_rest(bench: Bench) -> None:
    chain(bench)
    move(bench, "K")
    iv = bench.iv()
    assert iv.state("p.extract/X@v1").freshness == "invalidated"
    assert iv.state("p.compare/X@v1").freshness == "dirty"
    rec = iv.state("p.recommend/X@v1")
    assert rec.freshness == "dirty"
    assert rec.via == ("p.recommend/X@v1", "p.compare/X@v1", "p.extract/X@v1", "record:refs/K@1")
    (status,) = iv.state("p.extract/X@v1").changed_reads()
    assert (status.state, status.now) == ("moved", "2")


def test_a_gone_record_reads_gone(bench: Bench) -> None:
    chain(bench)
    move(bench, "K", gone=True)
    (status,) = bench.iv().state("p.extract/X@v1").reads
    assert status.state == "gone"


def test_early_cutoff_stops_the_cascade(bench: Bench) -> None:
    """test_equal_regeneration_raises_no_card: an equal machine-only rebuild moves nobody."""
    chain(bench)
    move(bench, "K")
    bench.rebuild_run("R-0002", ["R-0001"])
    bench.save("R-0002", "p.extract/X", {"v": 1}, reads=[record("K", "2")])
    iv = bench.iv()
    assert iv.state("p.extract/X@v2").freshness == "current"
    assert iv.state("p.compare/X@v1").freshness == "current_by_cutoff"
    assert iv.state("p.recommend/X@v1").freshness == "current"
    assert "interrupt.raised" not in bench.types() and "approval.revoked" not in bench.types()


def test_a_different_rebuild_invalidates_the_next_reader_only(bench: Bench) -> None:
    chain(bench)
    move(bench, "K")
    bench.rebuild_run("R-0002", ["R-0001"])
    bench.save("R-0002", "p.extract/X", {"v": 2}, reads=[record("K", "2")])
    iv = bench.iv()
    assert iv.state("p.compare/X@v1").freshness == "invalidated"
    assert iv.state("p.recommend/X@v1").freshness == "dirty"
    assert not iv.state("p.extract/X@v1").is_latest


def test_a_path_scoped_read_is_cut_off_when_its_entry_is_equal(bench: Bench) -> None:
    bench.start("R-0001")
    bench.save("R-0001", "p.extract/X", {"a": 1, "b": 1}, reads=[record("K")])
    whole = bench.latest("p.extract/X")
    assert whole is not None
    scoped = InputRef("artifact", "p.extract/X", "v1", "/a", entry_hash(whole.content, "/a"))
    bench.save("R-0001", "p.note/N", {"n": 1}, reads=[scoped])
    move(bench, "K")
    bench.rebuild_run("R-0002", ["R-0001"])
    bench.save("R-0002", "p.extract/X", {"a": 1, "b": 2}, reads=[record("K", "2")])
    assert bench.iv().state("p.note/N@v1").freshness == "current_by_cutoff"


def test_sibling_rule_an_unrelated_run_moves_nobody(bench: Bench) -> None:
    """S1: a second request writes the slot the first run's matrix read; no rebuild follows."""
    writer = WriterId("r", "fetch", "COMP-02")
    bench.start("R-0001", agent="r")
    bench.save("R-0001", "r.finding/COMP-02", {"region": "first"}, reads=[record("P")],
               writer=writer)  # fmt: skip
    bench.save("R-0001", "r.matrix", {"cells": 1}, reads=[a("r.finding/COMP-02@v1")])
    bench.end("R-0001")
    bench.start("R-0002", agent="r")
    bench.save("R-0002", "r.finding/COMP-02", {"region": "second"}, reads=[record("P")],
               writer=writer)  # fmt: skip
    iv = bench.iv()
    assert iv.state("r.matrix@v1").freshness == "current"
    assert iv.state("r.finding/COMP-02@v1").is_latest  # latest of its own line
    assert iv.state("r.finding/COMP-02@v2").is_latest


def test_sibling_rule_a_rebuild_for_the_readers_run_moves_it(bench: Bench) -> None:
    writer = WriterId("r", "fetch", "COMP-02")
    bench.start("R-0001", agent="r")
    bench.save("R-0001", "r.finding/COMP-02", {"p": 1}, reads=[record("P")], writer=writer)
    bench.save("R-0001", "r.matrix", {"cells": 1}, reads=[a("r.finding/COMP-02@v1")])
    bench.end("R-0001")
    bench.start("R-0002", agent="r")  # an unrelated request
    bench.end("R-0002")
    move(bench, "P")
    bench.start("R-0003", agent="r", cause="rebuild", for_runs=["R-0002"],
                rebuild_key="rebuild:o1/r/9")  # fmt: skip
    bench.save("R-0003", "r.finding/COMP-02", {"p": 2}, reads=[record("P", "2")], writer=writer)
    iv = bench.iv()
    assert iv.state("r.matrix@v1").freshness == "dirty"  # a rebuild for another run: sibling
    bench.start("R-0004", agent="r", cause="rebuild", for_runs=["R-0001"],
                rebuild_key="rebuild:o1/r/1")  # fmt: skip
    bench.save("R-0004", "r.finding/COMP-02", {"p": 3}, reads=[record("P", "2")], writer=writer)
    iv = bench.iv()
    assert iv.state("r.matrix@v1").freshness == "invalidated"
    assert runs_for(bench.view())["R-0004"] == frozenset({"R-0001"})


def test_cross_agent_reads_are_pinned_against_an_ordinary_request(bench: Bench) -> None:
    """ADR-P04 D11: a newer version from another agent's request does not move the reader."""
    bench.start("R-0001", agent="r")
    bench.save("R-0001", "r.finding/F", {"x": 1}, reads=[record("P")])
    bench.end("R-0001")
    bench.start("R-0002")
    bench.save("R-0002", "p.note/N", {"n": 1}, reads=[a("r.finding/F@v1")])
    bench.end("R-0002")
    bench.start("R-0003", agent="r")
    bench.save("R-0003", "r.finding/F", {"x": 2}, reads=[record("P")])
    assert bench.iv().state("p.note/N@v1").freshness == "current"


def test_a_withdrawn_writers_version_reads_gone(bench: Bench) -> None:
    """S3: a merged duplicate's version is gone to every reader."""
    dup = WriterId("r", "fetch", "COMP-06")
    bench.start("R-0001", agent="r")
    bench.save("R-0001", "r.finding/COMP-06", {"x": 1}, reads=[record("P")], writer=dup)
    bench.save("R-0001", "r.matrix", {"n": 1}, reads=[a("r.finding/COMP-06@v1")])
    iv = invalidation_view(bench.view(), withdrawn=[dup])
    assert iv.state("r.matrix@v1").freshness == "invalidated"
    assert iv.state("r.matrix@v1").reads[0].state == "gone"


# --- test_invalidation_one_pass_equals_fixpoint ------------------------------------------


def naive_fixpoint(view: LogView) -> dict[str, str]:
    """Iterate every version's state from 'current' until nothing changes (the answer key).

    Only for logs with one run and one agent: every newer version of a slot counts.
    """
    versions = all_versions(view)
    by_ref = {str(v.ref): v for v in versions}
    records: dict[str, str | None] = {}
    for e in view.events:
        if e.type == "source.changed":
            records.update(dict(e.payload["records"]))
    state = {r: "current" for r in by_ref}
    while True:
        new = {}
        for r, v in by_ref.items():
            reads = []
            for read in v.reads:
                if read.kind == "record":
                    now = records.get(read.id, read.version)
                    reads.append("same" if now == read.version else "moved")
                    continue
                target = by_ref[str(ArtifactRef.from_input(read))]
                later = [
                    n for n in versions
                    if n.ref.slot() == target.ref.slot() and n.ref.version > target.ref.version
                ]  # fmt: skip
                newest = max(later, key=lambda n: n.ref.version) if later else None
                stale = ("invalidated", "dirty")
                if newest is None:
                    reads.append("unsettled" if state[str(target.ref)] in stale else "same")
                elif newest.canonical_hash != target.canonical_hash:
                    reads.append("moved")
                else:
                    unsettled = state[str(newest.ref)] in stale
                    reads.append("unsettled" if unsettled else "equivalent")
            if "moved" in reads:
                new[r] = "invalidated"
            elif "unsettled" in reads:
                new[r] = "dirty"
            elif "equivalent" in reads:
                new[r] = "current_by_cutoff"
            else:
                new[r] = "current"
        if new == state:
            return state
        state = new


@pytest.mark.parametrize("seed", range(8))
def test_invalidation_one_pass_equals_fixpoint(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    b = Bench(tmp_path)
    try:
        b.start("R-0001")
        slots = [f"p.note/N{i}" for i in range(6)]
        saved: list[str] = []
        for n in range(18):
            slot = rng.choice(slots)
            reads: list[InputRef] = [record(f"K{rng.randrange(4)}")]
            for r in rng.sample(saved, k=min(len(saved), rng.randrange(3))):
                if not r.startswith(slot + "@"):
                    latest = b.latest(r.split("@")[0])
                    assert latest is not None
                    reads.append(latest.ref.as_input())
            b.save("R-0001", slot, {"v": rng.randrange(2)}, reads=reads, instance=f"i{n}")
            got = b.latest(slot)
            assert got is not None
            saved.append(str(got.ref))
            if rng.random() < 0.25:
                move(b, f"K{rng.randrange(4)}")
        view = b.view()
        iv = invalidation_view(view)
        assert {r: s.freshness for r, s in iv.versions.items()} == naive_fixpoint(view)
    finally:
        b.close()
