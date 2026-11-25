"""The hash chain's pure half (ADR-P01 Amendment H): records, hashes and the walk."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from aksha.runtime.canonical import canonical_bytes, freeze
from aksha.runtime.events.envelope import Event
from aksha.runtime.storage.chain import (
    CHAIN_VERSION,
    Anchor,
    ChainBreak,
    ChainLink,
    StoreIdentity,
    UnreadableArtifact,
    UnreadableLink,
    chain_record,
    content_hash,
    event_hash,
    genesis_hash,
    new_store_id,
    next_link,
    verify_chain,
)
from aksha.runtime.storage.errors import StoreError
from aksha.runtime.storage.rows import StoredArtifact
from tests.support.events import artifact_row

IDENTITY = StoreIdentity.create("st_unit", "2026-11-27T09:00:00.000000Z")


def _event(seq: int, n: int | None = None, owner: str = "o1") -> Event:
    return Event(
        type="probe.noted",
        payload=freeze({"n": seq if n is None else n, "text": "t"}),
        run="R-0001",
        dedup_key=f"probe:R-0001/{seq}",
        seq=seq,
        owner=owner,
        at="2026-11-27T09:00:00.000000Z",
        boot=1,
        schema_version=1,
    )


def _chain(count: int) -> list[ChainLink]:
    links: list[ChainLink] = []
    prev = IDENTITY.genesis_hash
    for seq in range(1, count + 1):
        link = next_link(_event(seq), prev)
        links.append(link)
        prev = link.hash
    return links


def _anchor(link: ChainLink, store_id: str = "st_unit") -> Anchor:
    return Anchor(store_id=store_id, seq=link.event.seq, hash=link.hash, at="t", sink="test")


def test_genesis_hash_covers_the_store_id_and_creation_time() -> None:
    expected = hashlib.sha256(
        b'{"chain":1,"created_at":"2026-11-27T09:00:00.000000Z","store_id":"st_unit"}'
    ).hexdigest()
    assert genesis_hash("st_unit", "2026-11-27T09:00:00.000000Z") == f"sha256:{expected}"
    assert IDENTITY.genesis_hash == f"sha256:{expected}"
    assert IDENTITY.chain_version == CHAIN_VERSION == 1
    assert genesis_hash("st_other", "2026-11-27T09:00:00.000000Z") != IDENTITY.genesis_hash


def test_event_hash_covers_the_whole_envelope() -> None:
    event = _event(1)
    record = chain_record(event, "sha256:" + "0" * 64)
    assert set(record) == {
        "chain", "seq", "prev_hash", "type", "payload", "at", "dedup_key",
        "owner", "run", "instance", "step", "session", "boot", "schema_version",
    }  # fmt: skip
    expected = hashlib.sha256(
        b'{"at":"2026-11-27T09:00:00.000000Z","boot":1,"chain":1,"dedup_key":"probe:R-0001/1",'
        b'"instance":null,"owner":"o1","payload":{"n":1,"text":"t"},"prev_hash":"sha256:'
        + b"0"
        * 64
        + b'","run":"R-0001","schema_version":1,"seq":1,"session":null,"step":null,'
        b'"type":"probe.noted"}'
    ).hexdigest()
    assert canonical_bytes(record).startswith(b'{"at":')
    assert event_hash(event, "sha256:" + "0" * 64) == f"sha256:{expected}"
    for changed in (
        replace(event, owner="o2"),
        replace(event, run="R-0002"),
        replace(event, at="2026-11-27T09:00:01.000000Z"),
        replace(event, boot=2),
        replace(event, schema_version=2),
        replace(event, dedup_key=None),
        replace(event, payload=freeze({"n": 1, "text": "u"})),
    ):
        assert event_hash(changed, "sha256:" + "0" * 64) != event_hash(event, "sha256:" + "0" * 64)


def test_next_link_chains_to_the_previous_hash() -> None:
    links = _chain(3)
    assert links[0].prev_hash == IDENTITY.genesis_hash
    assert [b.prev_hash for b in links[1:]] == [a.hash for a in links[:-1]]
    assert len({link.hash for link in links}) == 3


def test_new_store_ids_are_random_and_prefixed() -> None:
    a, b = new_store_id(), new_store_id()
    assert a != b
    assert a.startswith("st_") and len(a) == 3 + 32


def test_an_intact_chain_verifies() -> None:
    assert verify_chain(IDENTITY, _chain(5)) is None
    assert verify_chain(IDENTITY, []) is None


def test_an_edited_event_is_a_hash_mismatch_at_its_seq() -> None:
    links = _chain(5)
    links[2] = replace(links[2], event=_event(3, n=99))
    found = verify_chain(IDENTITY, links)
    recomputed = event_hash(_event(3, n=99), links[1].hash)
    assert found == ChainBreak(3, "hash_mismatch", recomputed, links[2].hash)


def test_swapped_events_break_at_the_first_moved_seq() -> None:
    links = _chain(5)
    a, b = links[1], links[2]
    links[1] = replace(b, event=replace(b.event, seq=2))
    links[2] = replace(a, event=replace(a.event, seq=3))
    found = verify_chain(IDENTITY, links)
    assert found is not None and found.seq == 2 and found.reason == "prev_hash_mismatch"


def test_a_deleted_middle_event_is_a_seq_gap_at_the_next_seq() -> None:
    links = _chain(5)
    del links[2]
    found = verify_chain(IDENTITY, links)
    assert found == ChainBreak(4, "seq_gap", "3", "4")


def test_a_wrong_genesis_is_reported_at_seq_zero() -> None:
    other = replace(IDENTITY, genesis_hash="sha256:" + "1" * 64)
    found = verify_chain(other, _chain(2))
    assert found is not None and (found.seq, found.reason) == (0, "genesis_mismatch")


def test_truncation_passes_without_an_anchor_and_fails_with_one() -> None:
    links = _chain(10)
    head = links[-1]
    assert verify_chain(IDENTITY, links[:7]) is None
    found = verify_chain(IDENTITY, links[:7], anchors=[_anchor(head)])
    assert found == ChainBreak(10, "truncated", head.hash, None)


def test_an_anchor_that_disagrees_is_an_anchor_mismatch() -> None:
    links = _chain(5)
    anchor = replace(_anchor(links[2]), hash="sha256:" + "2" * 64)
    found = verify_chain(IDENTITY, links, anchors=[anchor])
    assert found == ChainBreak(3, "anchor_mismatch", anchor.hash, links[2].hash)


def test_a_matching_anchor_passes_and_an_empty_store_matches_genesis() -> None:
    links = _chain(5)
    assert verify_chain(IDENTITY, links, anchors=[_anchor(links[4]), _anchor(links[1])]) is None
    genesis = Anchor(store_id="st_unit", seq=0, hash=IDENTITY.genesis_hash, at="t", sink="test")
    assert verify_chain(IDENTITY, [], anchors=[genesis]) is None


def test_an_anchor_of_another_store_is_refused() -> None:
    links = _chain(2)
    with pytest.raises(StoreError) as e:
        verify_chain(IDENTITY, links, anchors=[_anchor(links[1], store_id="st_other")])
    assert e.value.code == "anchor_store_mismatch"


def test_a_rehashed_rewrite_passes_alone_and_fails_against_an_anchor() -> None:
    links = _chain(6)
    anchor = _anchor(links[5])
    rewritten = links[:2]
    prev = links[1].hash
    for seq in range(3, 7):
        link = next_link(_event(seq, n=100 + seq), prev)
        rewritten.append(link)
        prev = link.hash
    assert verify_chain(IDENTITY, rewritten) is None
    found = verify_chain(IDENTITY, rewritten, anchors=[anchor])
    assert found is not None and (found.seq, found.reason) == (6, "anchor_mismatch")


def test_a_value_outside_the_encoding_is_reported_not_raised() -> None:
    links = _chain(2)
    links[1] = replace(links[1], event=replace(links[1].event, payload=freeze({"n": 1.5})))
    found = verify_chain(IDENTITY, links)
    assert found is not None and (found.seq, found.reason) == (2, "not_canonical")


def test_an_anchor_with_a_negative_seq_is_refused() -> None:
    links = _chain(2)
    bad = replace(_anchor(links[1]), seq=-1)
    with pytest.raises(StoreError) as e:
        verify_chain(IDENTITY, links, anchors=[bad])
    assert e.value.code == "anchor_invalid"


def test_the_genesis_is_recomputed_with_the_stored_chain_version() -> None:
    assert genesis_hash("s", "t") == genesis_hash("s", "t", CHAIN_VERSION)
    assert genesis_hash("s", "t", 2) != genesis_hash("s", "t")


def test_reading_an_identity_refuses_another_chain_version() -> None:
    kept = StoreIdentity.read("st_unit", IDENTITY.created_at, 1, IDENTITY.genesis_hash)
    assert kept == IDENTITY
    with pytest.raises(StoreError) as e:
        StoreIdentity.read("st_unit", IDENTITY.created_at, 2, IDENTITY.genesis_hash)
    assert e.value.code == "chain_version_unsupported"


def test_an_unreadable_row_is_checked_for_its_link_then_reported() -> None:
    links: list[ChainLink | UnreadableLink] = [*_chain(3)]
    good = links[1]
    assert isinstance(good, ChainLink)
    links[1] = UnreadableLink(2, good.prev_hash, good.hash, "payload is not JSON")
    assert verify_chain(IDENTITY, links) == ChainBreak(
        2, "not_canonical", None, "payload is not JSON"
    )
    links[1] = UnreadableLink(2, "sha256:" + "0" * 64, good.hash, "payload is not JSON")
    found = verify_chain(IDENTITY, links)
    assert found is not None and (found.seq, found.reason) == (2, "prev_hash_mismatch")


def _saved_chain() -> tuple[list[ChainLink], StoredArtifact]:
    """Two links, the second an ``artifact.saved`` for :func:`artifact_row`."""
    row = artifact_row()
    first = next_link(_event(1), IDENTITY.genesis_hash)
    payload = {"ref": row.ref, "kind": row.kind, "key": row.key, "version": row.version,
               "content_hash": row.content_hash}  # fmt: skip
    event = replace(_event(2), type="artifact.saved", payload=freeze(payload), dedup_key=None)
    return [first, next_link(event, first.hash)], StoredArtifact("o1", 2, row)


def test_an_artifact_row_must_match_its_saved_event() -> None:
    links, stored = _saved_chain()
    assert verify_chain(IDENTITY, links, artifacts=[stored]) is None
    for row in (
        replace(stored.row, content={"body": "edited"}),
        replace(stored.row, content_hash=content_hash({"body": "edited"})),
        replace(stored.row, version=2),
    ):
        found = verify_chain(IDENTITY, links, artifacts=[replace(stored, row=row)])
        assert found is not None and (found.seq, found.reason) == (2, "artifact_mismatch")
    for other in (replace(stored, owner="o9"), replace(stored, event_seq=1)):
        found = verify_chain(IDENTITY, links, artifacts=[other])
        assert found is not None and found.reason == "artifact_mismatch"
    unreadable = UnreadableArtifact("o1", stored.ref, 2, "content is not JSON")
    found = verify_chain(IDENTITY, links, artifacts=[unreadable])
    assert found == ChainBreak(
        2, "artifact_mismatch", stored.row.content_hash, "content is not JSON"
    )


def test_an_artifact_row_with_no_event_is_reported_at_its_seq() -> None:
    links, stored = _saved_chain()
    found = verify_chain(IDENTITY, links, artifacts=[stored, replace(stored, event_seq=5)])
    assert found == ChainBreak(5, "artifact_mismatch", None, "no event at this seq")
    gap = [links[0], replace(links[1], event=replace(links[1].event, seq=3))]
    found = verify_chain(IDENTITY, gap, artifacts=[stored])
    assert found == ChainBreak(2, "artifact_mismatch", None, "no event at this seq")


def test_a_saved_event_with_no_artifact_row_is_a_mismatch() -> None:
    """H6: a deleted artifact row breaks the chain at its ``artifact.saved`` event."""
    links, stored = _saved_chain()
    found = verify_chain(IDENTITY, links, artifacts=[])
    assert found == ChainBreak(
        2, "artifact_mismatch", stored.row.content_hash, "no artifact row for this event"
    )


def test_content_hash_uses_the_chain_encoding() -> None:
    assert (
        content_hash({"b": 1, "a": "x"})
        == "sha256:" + hashlib.sha256(b'{"a":"x","b":1}').hexdigest()
    )
    with pytest.raises(ValueError):
        content_hash({"x": 0.5})
