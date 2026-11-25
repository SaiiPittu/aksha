"""The ``BlobStore`` contract (decision 25): content-addressed, write-once, verified on read."""

from __future__ import annotations

import hashlib
import threading

import pytest

from aksha.runtime.artifacts.errors import BlobError
from aksha.runtime.artifacts.ports import BlobStore


def _code(blobs: BlobStore, ref: str) -> str:
    with pytest.raises(BlobError) as e:
        blobs.get(ref)
    return e.value.code


def test_blob_roundtrip_and_dedup(blobs: BlobStore) -> None:
    """Two puts of the same bytes give one ref; the bytes read back are the bytes put."""
    data = b"a test log\n" * 1000
    ref = blobs.put(data)
    assert ref == "sha256:" + hashlib.sha256(data).hexdigest()
    assert blobs.put(data) == ref
    assert blobs.get(ref) == data and blobs.has(ref)


def test_empty_and_binary_bytes(blobs: BlobStore) -> None:
    for data in (b"", bytes(range(256)), b"\x00" * 10):
        assert blobs.get(blobs.put(data)) == data


def test_different_bytes_have_different_refs(blobs: BlobStore) -> None:
    assert blobs.put(b"one") != blobs.put(b"two")


def test_missing_blob_is_not_found(blobs: BlobStore) -> None:
    ref = "sha256:" + "0" * 64
    assert not blobs.has(ref)
    assert _code(blobs, ref) == "blob_not_found"


@pytest.mark.parametrize(
    "ref",
    ["", "sha256:", "sha256:" + "A" * 64, "md5:" + "0" * 32, "sha256:../../etc/passwd",
     "sha256:" + "0" * 63, "sha256:" + "0" * 64 + "/x", "blob:sha256:" + "0" * 64],
)  # fmt: skip
def test_a_ref_that_is_no_blob_name_is_refused(blobs: BlobStore, ref: str) -> None:
    assert _code(blobs, ref) == "blob_ref_invalid"
    with pytest.raises(BlobError):
        blobs.has(ref)


def test_concurrent_puts_of_one_blob_agree(blobs: BlobStore) -> None:
    data = b"x" * 100_000
    refs: list[str] = []
    threads = [threading.Thread(target=lambda: refs.append(blobs.put(data))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(refs)) == 1 and blobs.get(refs[0]) == data
