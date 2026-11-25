from aksha.runtime.errors import AkshaError
from aksha.runtime.events.envelope import Event
from aksha.runtime.storage.errors import (
    AlreadyRecorded,
    ArtifactWithoutEvent,
    LockHeld,
    NotWriter,
    SequenceConflict,
    StoreError,
)


def _event() -> Event:
    return Event(
        type="probe.noted", payload={}, seq=3, owner="o1", at="", boot=1, schema_version=1,
        dedup_key="probe:R/1",
    )  # fmt: skip


def test_every_storage_error_is_a_store_error_with_a_code() -> None:
    errors: list[StoreError] = [
        AlreadyRecorded(_event()),
        LockHeld(pid=42),
        NotWriter(),
        SequenceConflict(owner="o1", expected=1, actual=2),
        ArtifactWithoutEvent("note/A@v1"),
    ]
    codes = [e.code for e in errors]
    assert codes == [
        "already_recorded", "lock_held", "not_writer", "sequence_conflict",
        "artifact_without_event",
    ]  # fmt: skip
    assert all(isinstance(e, AkshaError) for e in errors)


def test_already_recorded_carries_the_existing_event() -> None:
    e = AlreadyRecorded(_event())
    assert e.existing.seq == 3
    assert "probe:R/1" in str(e)


def test_lock_held_exits_8_and_names_the_holder() -> None:
    e = LockHeld(pid=42)
    assert (e.pid, e.exit_code) == (42, 8)
    assert "42" in str(e)
    assert LockHeld(pid=None).pid is None


def test_sequence_conflict_names_both_sequences() -> None:
    e = SequenceConflict(owner="o1", expected=1, actual=2)
    assert (e.owner, e.expected, e.actual) == ("o1", 1, 2)
