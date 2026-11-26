"""Payload offload (decision 25): large values move to the blob store and come back whole."""

from __future__ import annotations

from pathlib import Path

import pytest

from aksha.adapters.blobs.filesystem import FilesystemBlobStore
from aksha.runtime.artifacts.blobs import (
    MIN_OFFLOAD_BYTES,
    blob_problems,
    inflate,
    offload,
    offload_events,
)
from aksha.runtime.artifacts.errors import BlobError
from aksha.runtime.canonical import canonical_json, thaw
from aksha.runtime.events.catalogue import PAYLOAD_CAP_BYTES, Catalogue, EventTypeSpec
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.events.errors import PayloadInvalid, PayloadTooLarge
from aksha.runtime.events.stubs import JSON_TYPE, TEXT_TYPE, is_blob_stub
from aksha.runtime.projections.view import load_view
from tests.support.artifacts import OWNER, World
from tests.support.events import TEST_TYPES, probe_catalogue

LOG = EventTypeSpec(
    name="probe.logged",
    schema={
        "type": "object",
        "properties": {
            "n": {"type": "integer"},
            "log": {"type": "string", "minLength": 1},
            "rows": {"type": "array", "items": {"type": "integer"}},
            "tag": {"type": "string"},
        },
        "required": ["n", "log"],
        "additionalProperties": False,
    },
    writer="tests",
    boundaries=frozenset({"U2"}),
    dedup_key="plog:{n}/{tag}",
)
BIG = "line of a test log\n" * 5000  # about 95 KB


@pytest.fixture
def catalogue() -> Catalogue:
    from aksha.runtime.events.catalogue import CORE_EVENT_TYPES

    return Catalogue((*CORE_EVENT_TYPES, *TEST_TYPES, LOG))


@pytest.fixture
def blobs(tmp_path: Path) -> FilesystemBlobStore:
    return FilesystemBlobStore(tmp_path / "blobs")


def _size(payload: object) -> int:
    return len(canonical_json(payload).encode())


def test_a_payload_under_the_cap_is_unchanged(
    catalogue: Catalogue, blobs: FilesystemBlobStore
) -> None:
    payload = {"n": 1, "log": "short"}
    assert offload(catalogue, blobs, "probe.logged", payload) is payload


def test_the_largest_string_moves_and_the_event_keeps_a_stub(
    catalogue: Catalogue, blobs: FilesystemBlobStore
) -> None:
    payload = {"n": 1, "log": BIG, "tag": "x" * 2000}
    out = offload(catalogue, blobs, "probe.logged", payload)
    assert set(out["log"]) == {"blob", "bytes", "type"} and is_blob_stub(out["log"])
    assert out["log"]["type"] == TEXT_TYPE and out["log"]["bytes"] == len(BIG.encode())
    assert out["tag"] == "x" * 2000  # a dedup field never moves, and one move was enough
    assert _size(out) <= PAYLOAD_CAP_BYTES
    assert blobs.get(out["log"]["blob"]) == BIG.encode()
    assert thaw(inflate(out, blobs)) == payload


def test_offload_is_deterministic(catalogue: Catalogue, blobs: FilesystemBlobStore) -> None:
    payload = {"n": 1, "log": BIG, "rows": list(range(30_000))}
    assert offload(catalogue, blobs, "probe.logged", payload) == offload(
        catalogue, blobs, "probe.logged", payload
    )


def test_an_array_moves_when_no_large_string_is_left(
    catalogue: Catalogue, blobs: FilesystemBlobStore
) -> None:
    payload = {"n": 1, "log": "short", "rows": list(range(30_000))}
    out = offload(catalogue, blobs, "probe.logged", payload)
    assert out["rows"]["type"] == JSON_TYPE and thaw(inflate(out, blobs)) == payload


def test_the_whole_payload_is_checked_before_anything_moves(
    catalogue: Catalogue, blobs: FilesystemBlobStore
) -> None:
    with pytest.raises(PayloadInvalid):
        offload(catalogue, blobs, "probe.logged", {"n": "one", "log": BIG})
    assert not any((blobs.root / "sha256").iterdir())


def test_a_payload_with_nothing_left_to_move_is_still_refused(
    catalogue: Catalogue, blobs: FilesystemBlobStore
) -> None:
    with pytest.raises(PayloadTooLarge):
        offload(catalogue, blobs, "probe.logged", {"n": 1, "log": "x", "tag": "t" * 70_000})
    small = {"n": 1, "log": "x", "rows": list(range(10))}
    tiny = offload(catalogue, blobs, "probe.logged", small, cap=10 * MIN_OFFLOAD_BYTES)
    assert tiny == small


def test_inflate_needs_the_blob_store_only_for_a_stub(
    catalogue: Catalogue, blobs: FilesystemBlobStore
) -> None:
    plain = {"n": 1}
    assert inflate(plain, None) is plain
    out = offload(catalogue, blobs, "probe.logged", {"n": 1, "log": BIG})
    with pytest.raises(BlobError) as e:
        inflate(out, None)
    assert e.value.code == "blob_store_required"


def test_a_stubbed_event_appends_and_the_blob_half_of_the_chain_is_checked(
    tmp_path: Path,
) -> None:
    """The stored event keeps the stub; ``blob_problems`` finds a missing or edited blob."""
    world = World(tmp_path)
    try:
        catalogue = world.store.catalogue
        event = NewEvent("step.completed", {"kind": "tool", "name": "run", "outcome": "ok",
                                            "attempts": 1, "value": {"stdout": BIG}},
                         run="R-0001", step="R-0001/a/t01/c01")  # fmt: skip
        (moved,) = offload_events(catalogue, world.blobs, [event])
        world.store.append(OWNER, "U2", [moved])
        (stored,) = load_view(world.store, OWNER).events
        assert is_blob_stub(stored.payload["value"]["stdout"])
        assert inflate(stored.payload, world.blobs)["value"]["stdout"] == BIG
        assert blob_problems([stored], world.blobs) == ()
        path = world.blobs.path_of(stored.payload["value"]["stdout"]["blob"])
        path.chmod(0o644)
        path.write_bytes(b"edited")
        assert blob_problems([stored], world.blobs) == (
            (stored.seq, stored.payload["value"]["stdout"]["blob"], "blob_corrupt"),
        )
        path.unlink()
        assert blob_problems([stored], world.blobs)[0][2] == "blob_not_found"
    finally:
        world.close()


def test_a_stub_skips_only_the_schema_checks_under_it() -> None:
    catalogue = probe_catalogue()
    stub = {"blob": "sha256:" + "0" * 64, "bytes": 3, "type": TEXT_TYPE}
    catalogue.validate_payload("probe.noted", {"n": 1, "text": stub})
    with pytest.raises(PayloadInvalid):
        catalogue.validate_payload("probe.noted", {"n": "1", "text": stub})
    with pytest.raises(PayloadInvalid):
        catalogue.validate_payload("probe.noted", {"n": 1, "text": {**stub, "extra": 1}})
