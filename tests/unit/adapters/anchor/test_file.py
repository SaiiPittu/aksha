"""``FileAnchorSink``: anchors as JSON Lines in a file outside the store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aksha.adapters.anchor.file import FileAnchorSink
from aksha.runtime.storage.chain import Anchor
from aksha.runtime.storage.errors import StoreError

H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64


def _anchor(seq: int, hash_: str = H1, store_id: str = "st_a") -> Anchor:
    return Anchor(store_id=store_id, seq=seq, hash=hash_, at="2026-11-27T09:00:00.000000Z", sink="")


def test_publish_then_read_returns_anchors_oldest_first(tmp_path: Path) -> None:
    sink = FileAnchorSink(tmp_path / "audit" / "anchors.jsonl")
    assert sink.name == f"file:{tmp_path / 'audit' / 'anchors.jsonl'}"
    assert sink.read("st_a") == ()
    later = sink.publish(_anchor(9, H2))
    earlier = sink.publish(_anchor(4))
    assert later.sink == sink.name and later.receipt is None
    assert sink.read("st_a") == (earlier, later)
    lines = (tmp_path / "audit" / "anchors.jsonl").read_text().splitlines()
    assert [json.loads(line)["seq"] for line in lines] == [9, 4]


def test_publish_is_idempotent_on_store_and_seq(tmp_path: Path) -> None:
    sink = FileAnchorSink(tmp_path / "anchors.jsonl")
    first = sink.publish(_anchor(4))
    assert sink.publish(_anchor(4)) == first
    assert len(sink.read("st_a")) == 1


def test_a_second_hash_for_the_same_seq_is_refused(tmp_path: Path) -> None:
    sink = FileAnchorSink(tmp_path / "anchors.jsonl")
    sink.publish(_anchor(4))
    with pytest.raises(StoreError) as e:
        sink.publish(_anchor(4, H2))
    assert e.value.code == "anchor_conflict"


def test_read_filters_by_store(tmp_path: Path) -> None:
    sink = FileAnchorSink(tmp_path / "anchors.jsonl")
    sink.publish(_anchor(1, store_id="st_a"))
    sink.publish(_anchor(1, store_id="st_b"))
    assert [a.store_id for a in sink.read("st_b")] == ["st_b"]


def test_a_damaged_anchor_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "anchors.jsonl"
    path.write_text("not json\n")
    with pytest.raises(StoreError) as e:
        FileAnchorSink(path).read("st_a")
    assert e.value.code == "anchor_file_invalid"
