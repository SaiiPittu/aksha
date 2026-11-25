from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.events.catalogue import Catalogue
from aksha.runtime.projections.view import ArtifactsView, LogView, load_view
from aksha.runtime.storage.rows import StoredArtifact
from tests.support.events import artifact_row, probe, saved


def _stored(ref: str, seq: int) -> StoredArtifact:
    return StoredArtifact(owner="o1", event_seq=seq, row=artifact_row(ref))


def test_artifacts_view_hides_rows_after_its_cut() -> None:
    view = ArtifactsView((_stored("a@v1", 2), _stored("b@v1", 5)))
    assert view.seq_read == 5
    assert view.get("b@v1") is not None
    early = view.upto(3)
    assert early.get("b@v1") is None
    assert early.get("a@v1") == _stored("a@v1", 2)
    assert [a.ref for a in early.all()] == ["a@v1"]
    assert len(early) == 1 and len(view) == 2
    with pytest.raises(ValueError):
        early.upto(4)  # a view never grows


def test_log_view_upto_cuts_events_and_artifacts(
    tmp_path: Path, clock: FakeClock, catalogue: Catalogue
) -> None:
    store = open_store(tmp_path / "aksha.sqlite", clock, writer=True, catalogue=catalogue)
    try:
        store.append("o1", "U2", [probe(1)])
        store.append("o1", "U2", [probe(2), saved()], artifacts=[artifact_row()])
        store.append("o2", "U2", [probe(1)])
        view = load_view(store, "o1")
    finally:
        store.close()
    assert isinstance(view, LogView)
    assert (view.owner, view.seq_read) == ("o1", 3)
    assert [e.seq for e in view.events] == [1, 2, 3]
    assert view.artifacts.get("note/A@v1") is not None
    cut = view.upto(2)
    assert (cut.seq_read, [e.seq for e in cut.events]) == (2, [1, 2])
    assert cut.artifacts.get("note/A@v1") is None
    with pytest.raises(ValueError):
        view.upto(9)


def test_load_view_upto_an_earlier_sequence(
    tmp_path: Path, clock: FakeClock, catalogue: Catalogue
) -> None:
    store = open_store(tmp_path / "aksha.sqlite", clock, writer=True, catalogue=catalogue)
    try:
        store.append("o1", "U2", [probe(1)])
        store.append("o1", "U2", [probe(2)])
        assert [e.seq for e in load_view(store, "o1", upto_seq=1).events] == [1]
        assert load_view(store, "nobody").seq_read == 0
    finally:
        store.close()
