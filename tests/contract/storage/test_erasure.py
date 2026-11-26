"""Erasure in the append-only store (ADR-P30, amendments E1-E10): every storage adapter passes this.

A personal value appended inside a :class:`Personal` marker is stored in the ``erasables``
side table, and the event's hashed payload holds only a salted commitment to it. Erasing
a subject deletes its values and records ``erasure.performed``; the chain still verifies,
reads show the redacted stand-in, and no table holds the erased text any more.
"""

from __future__ import annotations

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.runtime.erasure.types import REDACTED, ErasureError, Personal
from aksha.runtime.events.catalogue import Catalogue
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.events.errors import BoundaryNotAllowed, PayloadInvalid
from aksha.runtime.storage.errors import AlreadyRecorded, NotWriter, StoreError
from aksha.runtime.storage.ports import EventStore
from tests.support.backends import Backend
from tests.support.events import artifact_row, probe, saved

NAME = "Ada Contributor"
EMPLOYER = "Example Instruments Ltd"


def _personal_probe(n: int, subject: str = "person:p1", text: str = NAME) -> NewEvent:
    return NewEvent("probe.noted", {"n": n, "text": Personal(subject, text)}, run="R-0001")


def _all_text(backend: Backend) -> str:
    """Every stored row of the event and side tables, as one string."""
    rows = [
        *backend.rows("SELECT * FROM events"),
        *backend.rows("SELECT * FROM erasables"),
        *backend.rows("SELECT * FROM artifacts"),
    ]
    return repr(rows)


def test_a_personal_value_reads_back_whole_and_the_chain_verifies(store: EventStore) -> None:
    stored = store.append("o1", "U2", [_personal_probe(1)])
    assert stored[0].payload["text"] == NAME
    assert [e.payload["text"] for e in store.read("o1")] == [NAME]
    assert store.verify() is None


def test_the_hashed_payload_holds_no_personal_text(store: EventStore, backend: Backend) -> None:
    store.append("o1", "U2", [_personal_probe(1)])
    events = repr(backend.rows("SELECT payload FROM events"))
    assert NAME not in events and "$erasable" in events
    assert NAME in repr(backend.rows("SELECT value FROM erasables"))


def test_erase_removes_the_value_everywhere_and_the_chain_still_verifies(
    store: EventStore, backend: Backend
) -> None:
    store.append("o1", "U2", [_personal_probe(1), probe(2)])
    store.append(
        "o1", "U2",
        [NewEvent("probe.free", {"items": [Personal("person:p1", EMPLOYER, redacted=None)]})],
    )  # fmt: skip
    store.append("o1", "U2", [_personal_probe(3, subject="person:p2", text="Bea Other")])
    receipt = store.erase("o1", "person:p1", reason="the subject asked")
    assert receipt.owner == "o1" and receipt.subject == "person:p1"
    assert len(receipt.refs) == 2 and receipt.seq == store.last_seq("o1")
    assert store.verify() is None
    payloads = [e.payload for e in store.read("o1")]
    assert payloads[0]["text"] == REDACTED
    assert payloads[2]["items"] == (None,)
    assert payloads[3]["text"] == "Bea Other"
    last = payloads[-1]
    assert last == {
        "subject": "person:p1",
        "refs": tuple(receipt.refs),
        "reason": "the subject asked",
    }
    text = _all_text(backend)
    assert NAME not in text and EMPLOYER not in text and "Bea Other" in text


def test_erase_is_idempotent_and_scoped_to_the_owner(store: EventStore) -> None:
    store.append("o1", "U2", [_personal_probe(1)])
    store.append("o2", "U2", [_personal_probe(1)])
    first = store.erase("o1", "person:p1", reason="request")
    again = store.erase("o1", "person:p1", reason="request")
    assert first.seq is not None and again.refs == () and again.seq is None
    assert store.last_seq("o1") == first.seq
    assert next(iter(store.read("o2"))).payload["text"] == NAME
    assert store.verify() is None


def test_a_reader_cannot_erase(backend: Backend, clock: FakeClock, catalogue: Catalogue) -> None:
    writer = backend.open(clock, writer=True, catalogue=catalogue)
    writer.append("o1", "U2", [_personal_probe(1)])
    reader = backend.open(clock, writer=False, catalogue=catalogue)
    try:
        with pytest.raises(NotWriter):
            reader.erase("o1", "person:p1", reason="request")
        assert next(iter(reader.read("o1"))).payload["text"] == NAME
    finally:
        reader.close()
        writer.close()


def test_erasure_is_seen_by_a_reader_opened_before_it(
    backend: Backend, clock: FakeClock, catalogue: Catalogue
) -> None:
    writer = backend.open(clock, writer=True, catalogue=catalogue)
    writer.append("o1", "U2", [_personal_probe(1)])
    reader = backend.open(clock, writer=False, catalogue=catalogue)
    try:
        writer.erase("o1", "person:p1", reason="request")
        assert next(iter(reader.read("o1"))).payload["text"] == REDACTED
        assert reader.verify() is None
    finally:
        reader.close()
        writer.close()


def test_a_value_deleted_without_an_erasure_breaks_the_chain(
    store: EventStore, backend: Backend
) -> None:
    store.append("o1", "U2", [probe(1)])
    store.append("o1", "U2", [_personal_probe(2)])
    backend.tamper("DELETE FROM erasables")
    broken = store.verify()
    assert broken is not None and (broken.seq, broken.reason) == (2, "erasable_missing")


def test_an_edited_value_breaks_the_chain_at_its_event(store: EventStore, backend: Backend) -> None:
    store.append("o1", "U2", [probe(1)])
    store.append("o1", "U2", [_personal_probe(2)])
    backend.tamper("UPDATE erasables SET value = '\"Someone Else\"'")
    broken = store.verify()
    assert broken is not None and (broken.seq, broken.reason) == (2, "erasable_mismatch")


def test_a_value_kept_after_its_erasure_breaks_the_chain(
    store: EventStore, backend: Backend
) -> None:
    store.append("o1", "U2", [_personal_probe(1)])
    kept = backend.rows("SELECT owner, ref, subject, salt, value, event_seq FROM erasables")
    store.erase("o1", "person:p1", reason="request")
    owner, ref, subject, salt, _, seq = kept[0]
    value_sql = '"' + NAME + '"'
    backend.tamper(
        "INSERT INTO erasables (owner, ref, subject, salt, value, event_seq) VALUES"
        f" ('{owner}', '{ref}', '{subject}', '{salt}', '{value_sql}', {seq})"
    )
    broken = store.verify()
    assert broken is not None and broken.reason == "erasable_mismatch"


def test_a_value_moved_to_another_event_breaks_the_chain(
    store: EventStore, backend: Backend
) -> None:
    store.append("o1", "U2", [_personal_probe(1)])
    store.append("o1", "U2", [probe(2)])
    backend.tamper("UPDATE erasables SET event_seq = 2")
    broken = store.verify()
    assert broken is not None and broken.reason == "erasable_mismatch"


def test_the_side_table_refuses_update(store: EventStore, backend: Backend) -> None:
    store.append("o1", "U2", [_personal_probe(1)])
    assert "append-only" in backend.refusal("UPDATE erasables SET subject = 'x'")


def test_a_dedup_key_that_reads_a_personal_value_is_refused(store: EventStore) -> None:
    event = NewEvent("probe.noted", {"n": Personal("person:p1", 7), "text": "t"}, run="R-0001")
    with pytest.raises(ErasureError) as e:
        store.append("o1", "U2", [event])
    assert e.value.code == "erasure_in_dedup_key"
    assert store.last_seq("o1") == 0


def test_a_redacted_stand_in_must_fit_the_schema(store: EventStore) -> None:
    event = NewEvent("probe.noted", {"n": 1, "text": Personal("person:p1", NAME, redacted=None)})
    with pytest.raises(PayloadInvalid):
        store.append("o1", "U2", [event])
    assert store.last_seq("o1") == 0


def test_personal_values_in_artifact_content_are_refused(store: EventStore) -> None:
    row = artifact_row()
    personal = type(row)(
        row.ref, row.kind, row.key, row.version, {"body": Personal("person:p1", NAME)},
        row.content_hash, row.canonical_hash,
    )  # fmt: skip
    with pytest.raises(StoreError) as e:
        store.append("o1", "U2", [saved()], artifacts=[personal])
    assert e.value.code == "erasure_in_artifact"


def test_already_recorded_carries_the_resolved_event(store: EventStore) -> None:
    store.append("o1", "U2", [_personal_probe(1)])
    with pytest.raises(AlreadyRecorded) as e:
        store.append("o1", "U2", [_personal_probe(1, text="Another Name")])
    assert e.value.existing.payload["text"] == NAME
    found = store.by_dedup_key("o1", "probe:R-0001/1")
    assert found is not None and found.payload["text"] == NAME


def test_erase_refuses_a_bad_subject_or_owner(store: EventStore) -> None:
    with pytest.raises(StoreError):
        store.erase("", "person:p1", reason="r")
    with pytest.raises(ErasureError):
        store.erase("o1", "", reason="r")


def test_erasure_of_one_subject_in_a_boundary_of_many_events(store: EventStore) -> None:
    store.append(
        "o1", "U2", [_personal_probe(1), _personal_probe(2), _personal_probe(3, "person:p2")]
    )
    receipt = store.erase("o1", "person:p1", reason="request")
    assert len(receipt.refs) == 2
    texts = [e.payload["text"] for e in store.read("o1", types=frozenset({"probe.noted"}))]
    assert texts == [REDACTED, REDACTED, NAME]
    assert store.verify() is None


def test_only_erase_writes_the_erasure_boundary(store: EventStore) -> None:
    store.append("o1", "U2", [_personal_probe(1)])
    forged = NewEvent(
        "erasure.performed", {"subject": "person:p1", "refs": ["er_" + "0" * 32], "reason": "r"}
    )
    with pytest.raises(StoreError) as e:
        store.append("o1", "U12", [forged])
    assert e.value.code == "erasure_boundary_reserved"
    with pytest.raises(BoundaryNotAllowed):
        store.append("o1", "U2", [forged])
    assert store.last_seq("o1") == 1


@pytest.mark.parametrize("reason", ["", "   ", "r\x00", "r" * 201])
def test_erase_refuses_a_bad_reason_before_it_looks_for_values(
    store: EventStore, reason: str
) -> None:
    with pytest.raises(ErasureError) as e:
        store.erase("o1", "person:p1", reason=reason)
    assert e.value.code == "erasure_reason_invalid"
    store.append("o1", "U2", [_personal_probe(1)])
    with pytest.raises(ErasureError):
        store.erase("o1", "person:p1", reason=reason)
    assert next(iter(store.read("o1"))).payload["text"] == NAME
    assert store.last_seq("o1") == 1
