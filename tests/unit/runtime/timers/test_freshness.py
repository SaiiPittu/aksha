"""Freshness lifetimes as facts (ADR-P25 D3): a recheck timer per fetch or save, and expiry."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.runtime.artifacts.types import ArtifactKind, InputRef, WriterId
from aksha.runtime.invalidation.plan import may_invalidate, rebuild_plan
from aksha.runtime.invalidation.sources import record_source_changes, source_facts
from aksha.runtime.invalidation.types import VersionedSourceSpec
from aksha.runtime.stop_policy.types import AgentScope
from aksha.runtime.timers.freshness import lifetimes, recheck_timers
from aksha.runtime.timers.view import pending
from tests.support.invalidation import OWNER, Bench, record, ref


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    b = Bench(tmp_path)
    yield b
    b.close()


def _expire(b: Bench, *targets: InputRef) -> None:
    events = record_source_changes(b.view(), (), cause="clock", expired=targets)
    assert events
    b.store.append(OWNER, "U8", events)


def test_lifetimes_map_each_collection_to_its_sources_max_age() -> None:
    specs = (
        VersionedSourceSpec("web", collections=("pages", "prices"), max_age_s=3600),
        VersionedSourceSpec("notes", collections=("notes",)),
    )
    assert dict(lifetimes(specs)) == {"pages": 3600, "prices": 3600}


def test_an_expired_record_invalidates_what_was_saved_before_it_only(bench: Bench) -> None:
    bench.start("R-0001")
    bench.save("R-0001", "p.extract/X", {"v": 1}, reads=[record("K")])
    bench.save("R-0001", "p.extract/Y", {"v": 1}, reads=[record("L")])
    _expire(bench, record("K"))
    (expired,) = [e for e in bench.events() if e.type == "source.expired"]
    assert list(expired.payload["records"]) == ["refs/K"] and expired.payload["cause"] == "clock"
    iv = bench.iv()
    x = iv.state("p.extract/X@v1")
    assert x.freshness == "invalidated"
    assert [(r.input, r.state, r.now) for r in x.changed_reads()] == [
        (record("K"), "moved", "expired")
    ]
    assert iv.state("p.extract/Y@v1").freshness == "current"
    assert may_invalidate(bench.view())
    (entry,) = rebuild_plan(iv)
    assert str(entry.invalidated) == "p.extract/X@v1"
    bench.save("R-0001", "p.extract/X", {"v": 2}, reads=[record("K")], instance="refetch")
    assert bench.iv().state("p.extract/X@v2").freshness == "current"


def test_an_expired_artifact_version_is_invalidated_and_its_readers_wait(bench: Bench) -> None:
    bench.start("R-0001")
    bench.save("R-0001", "p.extract/X", {"v": 1}, reads=[record("K")])
    bench.save("R-0001", "p.compare/X", {"rows": []}, reads=[ref("p.extract/X@v1").as_input()])
    _expire(bench, ref("p.extract/X@v1").as_input())
    iv = bench.iv()
    x = iv.state("p.extract/X@v1")
    assert x.freshness == "invalidated"
    (why,) = x.changed_reads()
    assert (why.input.kind, why.state, why.now) == ("time", "moved", "expired")
    assert iv.state("p.compare/X@v1").freshness == "dirty"
    assert source_facts(bench.view()).expired_artifacts == frozenset({"p.extract/X@v1"})


def test_expiry_counts_as_a_source_change(bench: Bench) -> None:
    _expire(bench, record("K"))
    _expire(bench, record("K"))
    facts = source_facts(bench.view())
    assert facts.source_version == 2
    assert [e.payload["source_version"] for e in bench.events() if e.type == "source.expired"] == [
        1,
        2,
    ]


def test_a_read_of_a_record_with_a_lifetime_schedules_one_open_recheck(bench: Bench) -> None:
    life = {"refs": 600}
    writer = WriterId("p", "collect", None)
    first = recheck_timers(bench.view(), (record("K"), record("L", "2")), (), life, {}, writer)
    targets = [record("K").as_json(), record("L", "2").as_json()]
    assert [e.payload["target"] for e in first] == targets
    assert {e.payload["after_ms"] for e in first} == {600_000}
    assert {e.payload["cause"] for e in first} == {"recheck"}
    bench.store.append(OWNER, "U2", first)
    again = recheck_timers(bench.view(), (record("K"),), (), life, {}, writer)
    assert again == ()
    assert recheck_timers(bench.view(), (record("M"),), (), {}, {}, writer) == ()


def test_a_save_of_a_kind_with_a_lifetime_schedules_a_recheck_of_that_version(
    bench: Bench,
) -> None:
    kinds = {"p.extract": ArtifactKind("extract", {"type": "object"}, keyed=True, max_age_s=60)}
    writer = WriterId("p", "collect", None)
    events = recheck_timers(bench.view(), (), (ref("p.extract/X@v1"),), {}, kinds, writer)
    (timer,) = events
    assert timer.payload["target"] == ref("p.extract/X@v1").as_input().as_json()
    assert timer.payload["after_ms"] == 60_000
    bench.store.append(OWNER, "U2", events)
    (open_,) = pending(bench.view())
    assert open_.spec.scope == AgentScope("p")


def test_a_recheck_closed_by_firing_lets_the_next_fetch_schedule_again(bench: Bench) -> None:
    from aksha.runtime.timers.facts import fired

    life = {"refs": 600}
    writer = WriterId("p", "collect", None)
    (first,) = recheck_timers(bench.view(), (record("K"),), (), life, {}, writer)
    bench.store.append(OWNER, "U2", (first,))
    bench.store.append(OWNER, "U8", (fired(str(first.payload["timer_id"]), "recheck"),))
    (second,) = recheck_timers(bench.view(), (record("K"),), (), life, {}, writer)
    assert second.payload["timer_id"] != first.payload["timer_id"]
    bench.store.append(OWNER, "U2", (second,))
    assert [t.spec.timer_id for t in pending(bench.view())] == [second.payload["timer_id"]]
