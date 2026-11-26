"""Each owner's input workspace: uploads become versioned records, and only what changed moves."""

from __future__ import annotations

from pathlib import Path

import pytest

from aksha.adapters.sources.workspace import InputWorkspace, UploadRefused


def test_an_upload_is_a_record_per_file_versioned_by_its_bytes(tmp_path: Path) -> None:
    ws = InputWorkspace("inputs", tmp_path / "w")
    changed = ws.put("o1", {"notes/a.md": b"one", "b.csv": b"x,y\n"})
    assert changed == ("input/b.csv", "input/notes/a.md")
    reader = ws.reader("o1")
    assert reader.records("inputs", "input") == ("input/b.csv", "input/notes/a.md")
    found = reader.read("inputs", "input/notes/a.md")
    assert found is not None and found.text == "one" and found.version.startswith("sha256:")
    v1 = ws.record_version("o1", "input/notes/a.md")
    assert ws.put("o1", {"notes/a.md": b"one"}) == ()  # the same bytes again: nothing moved
    assert ws.put("o1", {"notes/a.md": b"two"}) == ("input/notes/a.md",)
    assert ws.record_version("o1", "input/notes/a.md") not in (None, v1)
    assert ws.record_version("o1", "input/b.csv") is not None  # not uploaded again: kept


def test_a_write_stages_its_bytes_outside_every_owner_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between writing the bytes and the rename leaves no stray input record."""
    import os

    ws = InputWorkspace("inputs", tmp_path / "w")

    def crash(src: object, dst: object) -> None:
        raise OSError("crashed before the rename")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        ws.put("o1", {"notes/a.md": b"one"})
    assert ws.reader("o1").records("inputs", "input") == ()


def test_owners_never_see_each_others_uploads(tmp_path: Path) -> None:
    ws = InputWorkspace("inputs", tmp_path / "w")
    ws.put("o1", {"a.md": b"mine"})
    assert ws.reader("o2").records("inputs", "input") == ()
    assert ws.reader("o2").read("inputs", "input/a.md") is None
    assert ws.record_version("o2", "input/a.md") is None
    assert ws.version("o1") != ws.version("o2")


def test_a_configured_base_folder_is_seen_and_an_upload_of_the_same_path_wins(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    (base / "a.md").write_text("base", encoding="utf-8")
    (base / "b.md").write_text("base b", encoding="utf-8")
    ws = InputWorkspace("inputs", tmp_path / "w", base=base)
    assert ws.reader("o1").records("inputs", "input") == ("input/a.md", "input/b.md")
    ws.put("o1", {"a.md": b"uploaded"})
    found = ws.reader("o1").read("inputs", "input/a.md")
    assert found is not None and found.text == "uploaded"
    other = ws.reader("o2").read("inputs", "input/a.md")
    assert other is not None and other.text == "base"


def test_a_part_of_a_table_moves_only_when_its_rows_do(tmp_path: Path) -> None:
    ws = InputWorkspace("inputs", tmp_path / "w")
    ws.put("o1", {"t.csv": b"id,who\n1,a\n2,b\n"})
    part = "input/t.csv#where:who=a"
    v1 = ws.record_version("o1", part)
    ws.put("o1", {"t.csv": b"id,who\n1,a\n2,c\n"})
    assert ws.record_version("o1", part) == v1  # another row changed
    ws.put("o1", {"t.csv": b"id,who\n1,a\n3,a\n"})
    assert ws.record_version("o1", part) != v1


@pytest.mark.parametrize(
    "path", ["", "/abs.md", "../up.md", "a/../b.md", "a//b.md", "./a.md", "a\\b.md", "a#b.md"]
)
def test_an_unclean_path_is_refused_before_anything_is_written(tmp_path: Path, path: str) -> None:
    ws = InputWorkspace("inputs", tmp_path / "w")
    with pytest.raises(UploadRefused):
        ws.put("o1", {"ok.md": b"fine", path: b"x"})
    assert not (tmp_path / "w").exists()


def test_a_file_that_is_not_text_is_refused(tmp_path: Path) -> None:
    ws = InputWorkspace("inputs", tmp_path / "w")
    with pytest.raises(UploadRefused, match="UTF-8"):
        ws.put("o1", {"scan.pdf": b"%PDF-1.7\xff\xfe"})


def test_an_owner_name_that_is_no_plain_folder_name_is_hashed(tmp_path: Path) -> None:
    ws = InputWorkspace("inputs", tmp_path / "w")
    ws.put("../../etc", {"a.md": b"x"})
    assert [p.name for p in (tmp_path / "w").iterdir()] != ["etc"]
    assert all(p.is_relative_to(tmp_path / "w") for p in (tmp_path / "w").rglob("*"))
