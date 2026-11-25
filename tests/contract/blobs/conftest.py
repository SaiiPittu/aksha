"""Parametrise the blob store contract over every adapter: one ``blobs`` fixture each."""

from __future__ import annotations

from pathlib import Path

import pytest

from aksha.adapters.blobs.filesystem import FilesystemBlobStore
from aksha.runtime.artifacts.ports import BlobStore


@pytest.fixture(params=["filesystem"])
def blobs(request: pytest.FixtureRequest, tmp_path: Path) -> BlobStore:
    assert request.param == "filesystem"
    return FilesystemBlobStore(tmp_path / "blobs")
