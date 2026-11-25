import random
from pathlib import Path

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import SQLiteEventStore, open_store
from aksha.runtime.events.catalogue import Catalogue
from aksha.runtime.events.envelope import Event
from aksha.runtime.projections.cache import ProjectionCache
from aksha.runtime.projections.project import apply_events, project, rebuild
from aksha.runtime.projections.view import ArtifactsView, LogView, load_view
from tests.support.events import MEMORY_RECALLED, artifact_row, probe, probe_catalogue, saved
from tests.support.reducers import ProbeTrail, fill


def _store(path: Path, clock: FakeClock, catalogue: Catalogue) -> SQLiteEventStore:
    return open_store(path, clock, writer=True, catalogue=catalogue)


def test_rebuild_applies_only_declared_types_in_order(
    tmp_path: Path, clock: FakeClock, catalogue: Catalogue
) -> None:
    store = _store(tmp_path / "a.sqlite", clock, catalogue)
    try:
        fill(store, "o1", 30)
        view = load_view(store, "o1")
    finally:
        store.close()
    state = rebuild(ProbeTrail(), view)
    ns = [v for k, v in state if k == "n"]
    assert ns == sorted(ns) and ns
    # Every save sees its own artifact row: rows commit with their artifact.saved event.
    assert all(v == 1 for k, v in state if k != "n")


def test_apply_events_continues_from_a_state(
    tmp_path: Path, clock: FakeClock, catalogue: Catalogue
) -> None:
    store = _store(tmp_path / "a.sqlite", clock, catalogue)
    try:
        fill(store, "o1", 20)
        view = load_view(store, "o1")
    finally:
        store.close()
    reducer = ProbeTrail()
    half = view.upto(view.events[9].seq)
    later = view.events[10:]
    assert apply_events(reducer, rebuild(reducer, half), later, view.artifacts) == rebuild(
        reducer, view
    )


def test_projection_cache_equals_full_rebuild(
    tmp_path: Path, clock: FakeClock, catalogue: Catalogue
) -> None:
    """ADR-P01 `test_fold_cache_equals_fresh_fold`, renamed: cached equals rebuilt, every commit."""
    store = _store(tmp_path / "a.sqlite", clock, catalogue)
    cache = ProjectionCache()
    reducer = ProbeTrail()
    try:
        for i in range(25):
            fill(store, "o1", 1, seed=i, start=i)
            fill(store, "o2", 1, seed=100 + i, start=i)
            view = load_view(store, "o1")
            cached = project(reducer, view, cache=cache)
            assert project(reducer, view, cache=cache) is cached  # second read is a hit
            assert cached == rebuild(reducer, view)
            cache.clear()
            assert project(reducer, view, cache=cache) == cached
    finally:
        store.close()


def test_reducer_skips_unknown_types(tmp_path: Path, clock: FakeClock) -> None:
    with_ext = probe_catalogue()
    with_ext.register_extension(MEMORY_RECALLED, extension="memory")
    a = _store(tmp_path / "with.sqlite", clock, with_ext)
    b = _store(tmp_path / "without.sqlite", clock, probe_catalogue())
    try:
        fill(a, "o1", 40, seed=3)  # interleaves memory.recalled events
        fill(b, "o1", 40, seed=3)  # same rolls, no extension type in its catalogue
        view_a, view_b = load_view(a, "o1"), load_view(b, "o1")
    finally:
        a.close()
        b.close()
    assert any(e.type == "memory.recalled" for e in view_a.events)
    assert not any(e.type == "memory.recalled" for e in view_b.events)
    assert rebuild(ProbeTrail(), view_a) == rebuild(ProbeTrail(), view_b)


def test_incremental_equals_full_rebuild(
    tmp_path: Path, clock: FakeClock, catalogue: Catalogue
) -> None:
    """At 50 random cut points, the incremental path equals a full rebuild (ADR-P01 S2)."""
    store = _store(tmp_path / "a.sqlite", clock, catalogue)
    try:
        fill(store, "o1", 120, seed=11)
        full = load_view(store, "o1")
    finally:
        store.close()
    reducer = ProbeTrail()
    rng = random.Random(5)
    cuts = sorted(rng.sample([e.seq for e in full.events], 50))
    cache = ProjectionCache()
    for seq in cuts:
        view = full.upto(seq)
        incremental = project(reducer, view, cache=cache, incremental=True)
        assert incremental == rebuild(reducer, view)


class VisibleRows:
    """At each probe, how many artifact rows the reducer is shown."""

    name = "visible_rows"
    reads = frozenset({"probe.noted"})

    def initial(self, args: object) -> tuple[int, ...]:
        return ()

    def apply(
        self, state: tuple[int, ...], event: Event, artifacts: ArtifactsView
    ) -> tuple[int, ...]:
        return (*state, len(artifacts))


def test_each_event_sees_only_rows_committed_by_it(
    tmp_path: Path, clock: FakeClock, catalogue: Catalogue
) -> None:
    store = _store(tmp_path / "a.sqlite", clock, catalogue)
    try:
        store.append("o1", "U2", [probe(1)])
        store.append("o1", "U2", [probe(2), saved()], artifacts=[artifact_row()])  # row at seq 3
        store.append("o1", "U2", [probe(3)])
        view = load_view(store, "o1")
    finally:
        store.close()
    assert rebuild(VisibleRows(), view) == (0, 0, 1)


class CountingNone:
    """A reducer whose real state is None; counts how often it is rebuilt."""

    name = "always_none"
    reads = frozenset({"probe.noted"})

    def __init__(self) -> None:
        self.initials = 0

    def initial(self, args: object) -> None:
        self.initials += 1

    def apply(self, state: None, event: Event, artifacts: ArtifactsView) -> None:
        return None


def test_a_none_state_is_served_from_the_cache(
    tmp_path: Path, clock: FakeClock, catalogue: Catalogue
) -> None:
    store = _store(tmp_path / "a.sqlite", clock, catalogue)
    try:
        fill(store, "o1", 5)
        view = load_view(store, "o1")
    finally:
        store.close()
    reducer, cache = CountingNone(), ProjectionCache()
    assert project(reducer, view, cache=cache) is None
    assert project(reducer, view, cache=cache) is None
    assert reducer.initials == 1  # the second call was a hit


def test_a_filtered_view_never_shares_the_full_views_cache(
    tmp_path: Path, clock: FakeClock, catalogue: Catalogue
) -> None:
    store = _store(tmp_path / "a.sqlite", clock, catalogue)
    try:
        fill(store, "o1", 30)
        full = load_view(store, "o1")
        kept = frozenset({"probe.noted"})
        events = tuple(store.read("o1", upto_seq=full.seq_read, types=kept))
    finally:
        store.close()
    filtered = LogView("o1", full.seq_read, events[:3], full.artifacts)
    assert full.complete and not filtered.complete and full.upto(5).complete
    reducer, cache = ProbeTrail(), ProjectionCache()
    whole = project(reducer, full, cache=cache)
    assert project(reducer, filtered, cache=cache) == rebuild(reducer, filtered) != whole
    assert project(reducer, full, cache=cache) is whole  # the full entry was not replaced
