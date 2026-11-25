from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.runtime.invalidation.sources import record_source_changes, source_facts
from tests.support.invalidation import OWNER, Bench, FakeQueries, FakeSource, query, record


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    b = Bench(tmp_path)
    b.start("R-0001")
    b.save("R-0001", "p.extract/C1", {"n": 1}, reads=[record("K-1"), query("Q-1")])
    yield b
    b.close()


def test_nothing_moved_records_nothing_and_reexecutes_nothing(bench: Bench) -> None:
    queries = FakeQueries()
    events = record_source_changes(bench.view(), [FakeSource()], cause="recovery",
                                   run_query=queries)  # fmt: skip
    assert events == () and queries.calls == []


def test_a_moved_record_is_recorded_and_queries_are_reexecuted(bench: Bench) -> None:
    source, queries = FakeSource(), FakeQueries({(OWNER, "Q-1"): "h1"})
    source.change(OWNER, {"refs/K-1": None})
    events = record_source_changes(bench.view(), [source], cause="command", run_query=queries,
                                   change_set="cs")  # fmt: skip
    changed, rerun = events
    assert changed.type == "source.changed"
    assert dict(changed.payload["records"]) == {"refs/K-1": None}
    assert changed.payload["source_version"] == 1 and changed.payload["change_set"] == "cs"
    assert rerun.type == "query.reexecuted"
    assert (rerun.payload["previous"], rerun.payload["hash"], rerun.payload["changed"]) == (
        "h1", "h1", False,
    )  # fmt: skip


def test_source_facts_are_a_projection_of_the_recorded_change(bench: Bench) -> None:
    source = FakeSource()
    source.change(OWNER, {"refs/K-1": "2"})
    events = record_source_changes(bench.view(), [source], cause="command")
    bench.store.append(OWNER, "U8", events)
    facts = source_facts(bench.view())
    assert (facts.source_version, dict(facts.records), dict(facts.sources)) == (
        1, {"refs/K-1": "2"}, {"refs": "sv1"},
    )  # fmt: skip
    # The same version again moves nothing: the log already knows it.
    assert record_source_changes(bench.view(), [source], cause="recovery") == ()


def test_a_record_of_another_collection_is_not_asked(bench: Bench) -> None:
    other = FakeSource(name="pages", collections=("pages",))
    other.change(OWNER, {"refs/K-1": "9"})
    assert record_source_changes(bench.view(), [other], cause="command") == ()
