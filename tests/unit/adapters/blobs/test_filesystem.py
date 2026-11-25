"""``FilesystemBlobStore`` only: its layout, read-only files, corruption and repair."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from aksha.adapters.blobs.filesystem import FilesystemBlobStore
from aksha.runtime.artifacts.errors import BlobError


def test_layout_fans_out_by_the_first_two_hex(tmp_path: Path) -> None:
    store = FilesystemBlobStore(tmp_path)
    ref = store.put(b"hello")
    digest = ref.split(":")[1]
    path = tmp_path / "sha256" / digest[:2] / digest
    assert store.path_of(ref) == path and path.read_bytes() == b"hello"
    assert not stat.S_IMODE(path.stat().st_mode) & 0o222  # read-only
    assert [p.name for p in path.parent.iterdir()] == [digest]  # no temporary file left


def test_an_edited_blob_is_corrupt_and_a_put_repairs_it(tmp_path: Path) -> None:
    store = FilesystemBlobStore(tmp_path)
    ref = store.put(b"original")
    path = store.path_of(ref)
    os.chmod(path, 0o644)
    path.write_bytes(b"edited")
    with pytest.raises(BlobError) as e:
        store.get(ref)
    assert e.value.code == "blob_corrupt"
    assert store.put(b"original") == ref and store.get(ref) == b"original"


def test_the_root_is_created(tmp_path: Path) -> None:
    root = tmp_path / "a" / "b"
    assert FilesystemBlobStore(root).root == root and (root / "sha256").is_dir()
