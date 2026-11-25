from dataclasses import replace

import pytest

from aksha.runtime.events.catalogue import Catalogue
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.events.errors import (
    BoundaryNotAllowed,
    DedupKeyError,
    PayloadInvalid,
    UnknownEventType,
)
from aksha.runtime.storage.append import SYSTEM_OWNER, check_owner, prepare_append
from aksha.runtime.storage.errors import ArtifactWithoutEvent, StoreError
from tests.support.events import artifact_row, artifact_row_with, probe, saved


def test_prepare_renders_keys_and_canonical_payloads(catalogue: Catalogue) -> None:
    batch = prepare_append(catalogue, "o1", "U2", [probe(1), saved()], [artifact_row()])
    first, second = batch.events
    assert first.dedup_key == "probe:R-0001/1"
    assert first.payload_json == '{"n":1,"text":"t"}'
    assert first.schema_version == 1
    assert second.dedup_key == "save:note/A@v1"
    assert batch.artifacts == ((1, artifact_row()),)
    assert batch.dedup_keys() == ("probe:R-0001/1", "save:note/A@v1")


def test_prepare_refuses_unknown_types_and_wrong_boundaries(catalogue: Catalogue) -> None:
    with pytest.raises(UnknownEventType):
        prepare_append(catalogue, "o1", "U2", [NewEvent("nope.never", {})], ())
    with pytest.raises(BoundaryNotAllowed):
        prepare_append(catalogue, "o1", "U3", [probe(1)], ())
    with pytest.raises(StoreError) as e:
        prepare_append(catalogue, "o1", "U2", [], ())
    assert e.value.code == "empty_boundary"


def test_prepare_checks_caller_dedup_keys(catalogue: Catalogue) -> None:
    ok = NewEvent("probe.noted", {"n": 1, "text": "t"}, run="R", dedup_key="probe:R/1")
    assert prepare_append(catalogue, "o1", "U2", [ok], ()).events[0].dedup_key == "probe:R/1"
    wrong = NewEvent("probe.noted", {"n": 2, "text": "t"}, run="R", dedup_key="probe:R/9")
    with pytest.raises(DedupKeyError) as e:
        prepare_append(catalogue, "o1", "U2", [wrong], ())
    assert e.value.code == "dedup_key_mismatch"
    stray = NewEvent("probe.free", {}, dedup_key="free:1")
    with pytest.raises(DedupKeyError) as e:
        prepare_append(catalogue, "o1", "U2", [stray], ())
    assert e.value.code == "dedup_key_not_allowed"
    with pytest.raises(DedupKeyError) as e:
        prepare_append(catalogue, "o1", "U2", [probe(5), probe(5)], ())
    assert e.value.code == "dedup_key_repeated_in_boundary"


def test_prepare_pairs_every_artifact_row_with_its_saved_event(catalogue: Catalogue) -> None:
    with pytest.raises(ArtifactWithoutEvent) as e:
        prepare_append(catalogue, "o1", "U2", [probe(2)], [artifact_row("note/B@v1")])
    assert e.value.code == "artifact_without_event"


def test_system_owner_only_for_recovery(catalogue: Catalogue) -> None:
    with pytest.raises(StoreError) as e:
        prepare_append(catalogue, SYSTEM_OWNER, "U2", [probe(1)], ())
    assert e.value.code == "system_owner_reserved"
    done = NewEvent("recovery.completed", {"last_seq_before_boot": 0})
    assert prepare_append(catalogue, SYSTEM_OWNER, "U7", [done], ()).owner == SYSTEM_OWNER
    with pytest.raises(StoreError) as e:
        prepare_append(catalogue, "o1", "U7", [done], ())
    assert e.value.code == "system_owner_reserved"


def test_check_owner_refuses_an_empty_owner() -> None:
    assert check_owner("o1") == "o1"
    with pytest.raises(StoreError) as e:
        check_owner("")
    assert e.value.code == "owner_required"


def test_first_conflict_follows_batch_order(catalogue: Catalogue) -> None:
    batch = prepare_append(catalogue, "o1", "U2", [probe(1), probe(2), probe(3)], ())
    two = batch.events[1].stored(seq=7, owner="o1", at="t", boot=1)
    three = batch.events[2].stored(seq=5, owner="o1", at="t", boot=1)
    assert batch.first_conflict({"probe:R-0001/3": three, "probe:R-0001/2": two}) == two
    assert batch.first_conflict({}) is None


def test_prepared_event_becomes_a_stored_event(catalogue: Catalogue) -> None:
    (prepared,) = prepare_append(catalogue, "o1", "U2", [probe(1)], ()).events
    event = prepared.stored(seq=9, owner="o1", at="2026-11-27T09:00:00.000000Z", boot=2)
    assert (event.seq, event.owner, event.boot, event.dedup_key) == (9, "o1", 2, "probe:R-0001/1")
    assert event.payload == {"n": 1, "text": "t"}


def test_prepare_append_refuses_nul_in_any_stored_string(catalogue: Catalogue) -> None:
    for owner, events, rows, code in [
        ("o\x00", [probe(1)], [], "nul_in_field"),
        ("o", [probe(1, run="R\x00")], [], "nul_in_field"),
        ("o", [saved()], [artifact_row_with({"b": "\x00"})], "nul_in_content"),
    ]:
        with pytest.raises(StoreError) as e:
            prepare_append(catalogue, owner, "U2", events, rows)
        assert e.value.code == code


def test_prepare_append_refuses_lone_surrogates_before_any_transaction(
    catalogue: Catalogue,
) -> None:
    for owner, events, boundary in [
        ("o\ud800", [probe(1)], "U2"),
        ("o", [probe(1, run="R\udfff")], "U2"),
        ("o", [NewEvent("probe.free", {"items": []}, step="\ud800")], "U2"),
    ]:
        with pytest.raises(StoreError) as e:
            prepare_append(catalogue, owner, boundary, events, ())
        assert e.value.code == "not_canonical_field"


def test_prepare_append_binds_each_artifact_row_to_its_saved_event(
    catalogue: Catalogue,
) -> None:
    """H6: the row's content hashes to its content_hash, which the event carries."""
    edited = {"body": "edited"}
    for events, row, code in [
        ([saved()], artifact_row_with(edited), "content_hash_invalid"),
        ([saved()], artifact_row(content=edited), "content_hash_mismatch"),
        ([saved(content=edited)], artifact_row(), "content_hash_mismatch"),
        ([saved()], replace(artifact_row(), version=2), "content_hash_mismatch"),
        ([saved()], artifact_row_with({"x": 0.5}), "not_canonical_content"),
    ]:
        with pytest.raises(StoreError) as e:
            prepare_append(catalogue, "o1", "U2", events, [row])
        assert e.value.code == code, row
    with pytest.raises(PayloadInvalid) as p:
        prepare_append(catalogue, "o1", "U2", [NewEvent("artifact.saved", {"ref": "r",
            "kind": "note", "key": None, "version": 1})], ())  # fmt: skip
    assert p.value.path == "$"
    batch = prepare_append(
        catalogue, "o1", "U2", [saved(content=edited)], [artifact_row(content=edited)]
    )
    assert batch.artifacts[0][1].content_hash == saved(content=edited).payload["content_hash"]
