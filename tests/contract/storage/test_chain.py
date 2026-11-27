"""The hash chain contract (ADR-P01 Amendment H): every storage adapter passes this unchanged.

Each tamper test goes past the adapter with a raw connection, turns off the append-only
triggers (which only someone with database access can do), changes rows, and checks that
``verify()`` names the first broken seq. ``test_sqlite_and_postgres_hash_identically``
opens both backends at once and asserts byte-identical chains.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from aksha.adapters.anchor.file import FileAnchorSink
from aksha.adapters.clock.fake import FakeClock
from aksha.runtime.events.catalogue import Catalogue
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.events.errors import PayloadInvalid
from aksha.runtime.storage.anchors import anchor_head, verify_against
from aksha.runtime.storage.chain import ChainBreak, ChainLink, content_hash, event_hash
from aksha.runtime.storage.errors import AlreadyRecorded, StoreError
from aksha.runtime.storage.ports import EventStore
from tests.support.backends import Backend, PostgresBackend, SQLiteBackend
from tests.support.events import (
    ARTIFACT_CONTENT,
    START,
    artifact_row,
    probe,
    probe_catalogue,
    run_started,
    saved,
)

SCRIPT_STORE_ID = "st_contract"


def _fill(store: EventStore, count: int = 6) -> None:
    """``count`` events, one per boundary, alternating two owners."""
    for n in range(1, count + 1):
        store.append("o1" if n % 2 else "o2", "U2", [probe(n)])


def _script(store: EventStore, clock: FakeClock) -> None:
    """A fixed sequence of boundaries: two owners, a four-event boundary, a rolled-back one."""
    store.append("o1", "U1", [run_started()])
    clock.advance(1)
    store.append("o1", "U2", [probe(1), probe(2, text="é ü 😀"), probe(3), probe(4)])
    clock.advance(1)
    store.append("o2", "U2", [probe(1, text="other owner")])
    with pytest.raises(AlreadyRecorded):
        store.append("o1", "U2", [probe(5), probe(1)])
    clock.advance(1)
    store.append("o1", "U2", [NewEvent("probe.free", {"items": [1, -2, None, True, {"k": "v"}]})])
    store.append("o2", "U2", [NewEvent("probe.free", {"items": [AWKWARD]})])


AWKWARD: dict[str, Any] = {
    "\U0001f600": 1,  # outside the BMP: sorts before U+FB33 by UTF-16, after it by code point
    "\ufb33": 2,
    "a\u2028b": "\x1f\x7f",
    "a\nb": "\u2029",
    "e\u0301": "\u00e9",  # a combining mark is not normalised away
    "": [2**53, -(2**53)],
}
"""Keys and values where a backend-specific ordering or escaping bug would show."""


def test_chain_links_every_append(store: EventStore, backend: Backend) -> None:
    store.append("o1", "U2", [probe(n, run="R-4") for n in range(1, 5)])
    _fill(store, 3)
    rows = backend.rows("SELECT seq, prev_hash, hash FROM events ORDER BY seq")
    assert [r[0] for r in rows] == list(range(1, 8))
    assert rows[0][1] == store.identity().genesis_hash
    assert [r[1] for r in rows[1:]] == [r[2] for r in rows[:-1]]
    assert all(str(h).startswith("sha256:") and len(str(h)) == 71 for r in rows for h in r[1:])
    assert store.head() == (7, rows[-1][2])
    assert store.verify() is None


def test_tampered_row_detected(store: EventStore, backend: Backend) -> None:
    _fill(store)
    backend.tamper('UPDATE events SET payload = \'{"n":3,"text":"edited"}\' WHERE seq = 3')
    found = store.verify()
    assert found is not None and (found.seq, found.reason) == (3, "hash_mismatch")


def test_a_read_after_a_tamper_returns_the_stored_row_not_an_earlier_decoding(
    store: EventStore, backend: Backend
) -> None:
    """Reads reuse decoded rows (``RowMemo``) only while the stored row is unchanged."""
    _fill(store)
    store.append("o1", "U2", [saved()], artifacts=[artifact_row()])
    texts = {e.seq: e.payload.get("text") for e in store.read("o1")}
    assert texts[3] == "t"
    assert store.read_artifacts("o1")[0].row.content == ARTIFACT_CONTENT
    backend.tamper(
        'UPDATE events SET payload = \'{"n":3,"text":"edited"}\' WHERE seq = 3',
        """UPDATE artifacts SET content = '{"body":"edited"}'""",
    )
    assert {e.seq: e.payload.get("text") for e in store.read("o1")} == {**texts, 3: "edited"}
    assert store.read_artifacts("o1")[0].row.content == {"body": "edited"}


def test_owner_change_breaks_chain(store: EventStore, backend: Backend) -> None:
    _fill(store)
    backend.tamper("UPDATE events SET owner = 'o9' WHERE seq = 2")
    found = store.verify()
    assert found is not None and (found.seq, found.reason) == (2, "hash_mismatch")


def test_swapped_row_contents_detected(store: EventStore, backend: Backend) -> None:
    """Rows 3 and 4 trade payload and envelope; seq, prev_hash and hash stay put."""
    _fill(store)
    columns = ("owner", "type", "payload", "run", "instance", "step", "session", "at", "boot",
               "schema_version")  # fmt: skip
    swap = "FROM swap s WHERE s.seq = 7 - events.seq AND events.seq IN (3, 4)"
    backend.tamper(
        "CREATE TEMP TABLE swap AS SELECT * FROM events WHERE seq IN (3, 4)",
        f"UPDATE events SET {', '.join(f'{c} = s.{c}' for c in columns)}, uniq = NULL {swap}",
        f"UPDATE events SET uniq = s.uniq {swap}",
    )
    assert [r[0] for r in backend.rows("SELECT owner FROM events ORDER BY seq")][2:4] == [
        "o2",
        "o1",
    ]
    found = store.verify()
    assert found is not None and (found.seq, found.reason) == (3, "hash_mismatch")


def test_reordered_rows_detected(store: EventStore, backend: Backend) -> None:
    _fill(store)
    backend.tamper(
        "UPDATE events SET seq = 1000 WHERE seq = 3",
        "UPDATE events SET seq = 3 WHERE seq = 4",
        "UPDATE events SET seq = 4 WHERE seq = 1000",
    )
    found = store.verify()
    assert found is not None and (found.seq, found.reason) == (3, "prev_hash_mismatch")


def test_middle_deletion_detected(store: EventStore, backend: Backend) -> None:
    _fill(store)
    backend.tamper("DELETE FROM events WHERE seq = 3")
    assert store.verify() == ChainBreak(4, "seq_gap", "3", "4")


def test_truncation_undetected_without_anchor(store: EventStore, backend: Backend) -> None:
    """The limit, kept honest: a shorter chain is still a valid chain."""
    _fill(store, 12)
    backend.tamper("DELETE FROM events WHERE seq > 2")
    assert store.verify() is None


def test_truncation_detected_with_anchor(
    store: EventStore, backend: Backend, clock: FakeClock, tmp_path: Path
) -> None:
    _fill(store, 12)
    sink = FileAnchorSink(tmp_path / "audit" / "anchors.jsonl")
    anchor = anchor_head(store, sink, clock)
    assert (anchor.seq, anchor.hash) == store.head()
    assert anchor.store_id == store.identity().store_id
    assert verify_against(store, sink) is None
    backend.tamper("DELETE FROM events WHERE seq > 2")
    assert store.verify() is None
    assert verify_against(store, sink) == ChainBreak(12, "truncated", anchor.hash, None)
    assert store.verify(anchors=sink.read(anchor.store_id)) == verify_against(store, sink)


def test_rehashed_rewrite_detected_only_by_anchor(
    store: EventStore, backend: Backend, clock: FakeClock, tmp_path: Path
) -> None:
    _fill(store)
    sink = FileAnchorSink(tmp_path / "anchors.jsonl")
    anchor_head(store, sink, clock)
    backend.tamper('UPDATE events SET payload = \'{"n":3,"text":"edited"}\' WHERE seq = 3')
    # Re-hash seq 3 to the end, as someone with write access could.
    prev = str(backend.rows("SELECT hash FROM events WHERE seq = 2")[0][0])
    for seq in range(3, 7):
        (event,) = (e for owner in ("o1", "o2") for e in store.read(owner) if e.seq == seq)
        new = event_hash(event, prev)
        backend.tamper(f"UPDATE events SET prev_hash = '{prev}', hash = '{new}' WHERE seq = {seq}")
        prev = new
    assert store.verify() is None
    found = verify_against(store, sink)
    assert found is not None and (found.seq, found.reason) == (6, "anchor_mismatch")


def test_rolled_back_boundary_leaves_chain_intact(store: EventStore) -> None:
    store.append("o1", "U2", [probe(1), probe(2)])
    head = store.head()
    with pytest.raises(AlreadyRecorded):
        store.append("o1", "U2", [probe(3), probe(4), probe(2)])
    assert store.head() == head
    (e,) = store.append("o1", "U2", [probe(3)])
    assert e.seq == head[0] + 1  # no gap, even where a sequence object would leave one
    assert store.verify() is None


def test_a_boundary_failing_mid_insert_leaves_chain_intact(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first event of the boundary is inserted, then the second fails; all rolls back."""
    store.append("o1", "U2", [probe(1)])
    head = store.head()
    adapter = sys.modules[type(store).__module__]
    real = adapter.next_link
    linked: list[int] = []

    def fail_on_second(event: Any, prev: str) -> ChainLink:
        linked.append(event.seq)
        if len(linked) == 2:
            raise RuntimeError("the process died between two inserts")
        link: ChainLink = real(event, prev)
        return link

    monkeypatch.setattr(adapter, "next_link", fail_on_second)
    with pytest.raises(RuntimeError):
        store.append("o1", "U2", [probe(2), probe(3)])
    monkeypatch.undo()
    assert linked == [2, 3]
    assert store.head() == head
    assert store.last_seq("o1") == 1
    (e,) = store.append("o1", "U2", [probe(2)])
    assert e.seq == head[0] + 1
    assert store.verify() is None


def test_lone_surrogates_refused_before_the_transaction(store: EventStore) -> None:
    store.append("o1", "U2", [probe(1)])
    head = store.head()
    free = NewEvent("probe.free", {"items": []}, session="\ud800")
    for owner, event in (
        ("o1", probe(2, run="\ud800")),
        ("o1", free),
        ("o\udc00", probe(2)),
    ):
        with pytest.raises(StoreError) as e:
            store.append(owner, "U2", [probe(3), event])
        assert e.value.code == "not_canonical_field"
    assert store.head() == head
    assert store.verify() is None


@pytest.mark.parametrize(
    "edit",
    [
        'content = \'{"body":"edited"}\'',
        "content_hash = 'sha256:" + "0" * 64 + "'",
        'content = \'{"body":"edited"}\', content_hash = \''
        + content_hash({"body": "edited"})
        + "'",
        "kind = 'other'",
    ],
)
def test_artifact_row_edit_detected(store: EventStore, backend: Backend, edit: str) -> None:
    """H6: the artifact.saved event's content_hash covers the row it saved."""
    store.append("o1", "U2", [probe(99)])
    (event,) = store.append("o1", "U2", [saved()], artifacts=[artifact_row()])
    _fill(store, 2)
    assert store.verify() is None
    backend.tamper(f"UPDATE artifacts SET {edit}")
    found = store.verify()
    assert found is not None and (found.seq, found.reason) == (event.seq, "artifact_mismatch")
    assert found.expected == content_hash({"body": "hello"})


def test_deleted_artifact_row_detected(store: EventStore, backend: Backend) -> None:
    """H6: deleting the row an ``artifact.saved`` event committed with breaks verify()."""
    store.append("o1", "U2", [probe(99)])
    (event,) = store.append("o1", "U2", [saved()], artifacts=[artifact_row()])
    _fill(store, 2)
    assert store.verify() is None
    backend.tamper("DELETE FROM artifacts")
    assert store.verify() == ChainBreak(
        event.seq, "artifact_mismatch", content_hash({"body": "hello"}),
        "no artifact row for this event",
    )  # fmt: skip


def test_a_store_of_another_chain_version_is_refused(
    backend: Backend, clock: FakeClock, catalogue: Catalogue
) -> None:
    backend.open(clock, writer=True, catalogue=catalogue).close()
    backend.tamper("UPDATE store_meta SET chain_version = 2")
    for writer in (True, False):
        with pytest.raises(StoreError) as e:
            backend.open(clock, writer=writer, catalogue=catalogue)
        assert e.value.code == "chain_version_unsupported"


def test_append_refuses_a_head_moved_under_the_writer(store: EventStore, backend: Backend) -> None:
    _fill(store)
    backend.tamper("DELETE FROM events WHERE seq > 4")
    with pytest.raises(StoreError) as e:
        store.append("o1", "U2", [probe(99)])
    assert e.value.code == "chain_head_moved"


def test_verify_runs_on_a_reader(store: EventStore, backend: Backend, clock: FakeClock) -> None:
    _fill(store)
    reader = backend.open(clock, writer=False, catalogue=store.catalogue)
    try:
        assert reader.identity() == store.identity()
        assert reader.head() == store.head()
        assert reader.verify() is None
        backend.tamper("DELETE FROM events WHERE seq = 2")
        assert reader.verify() == ChainBreak(3, "seq_gap", "2", "3")
    finally:
        reader.close()


def test_an_empty_store_heads_at_its_genesis(store: EventStore) -> None:
    assert store.head() == (0, store.identity().genesis_hash)
    assert store.verify() is None


def test_store_identity_is_written_once_and_kept(
    backend: Backend, clock: FakeClock, catalogue: Catalogue
) -> None:
    first = backend.open(clock, writer=True, catalogue=catalogue, store_id="st_given")
    identity = first.identity()
    first.close()
    assert identity.store_id == "st_given"
    assert identity.created_at == "2026-11-27T09:00:00.000000Z"
    clock.advance(60)
    again = backend.open(clock, writer=True, catalogue=catalogue)
    try:
        assert again.identity() == identity
    finally:
        again.close()
    with pytest.raises(StoreError) as e:
        backend.open(clock, writer=True, catalogue=catalogue, store_id="st_other")
    assert e.value.code == "store_id_mismatch"
    for sql in ("UPDATE store_meta SET store_id = 'x'", "DELETE FROM store_meta"):
        assert "append-only" in backend.refusal(sql)


def test_new_stores_get_distinct_ids(
    backend: Backend, clock: FakeClock, catalogue: Catalogue, tmp_path: Path
) -> None:
    store = backend.open(clock, writer=True, catalogue=catalogue)
    try:
        first = store.identity().store_id
    finally:
        store.close()
    other = SQLiteBackend(tmp_path / "second").open(clock, writer=True, catalogue=catalogue)
    try:
        second = other.identity().store_id
    finally:
        other.close()
    assert first.startswith("st_") and second.startswith("st_")
    assert first != second


def test_floats_and_unsafe_integers_refused_alike(store: EventStore) -> None:
    for payload, path in (
        ({"items": [1, 2.5]}, "$.items[1]"),
        ({"items": [2**53 + 1]}, "$.items[0]"),
    ):
        with pytest.raises(PayloadInvalid) as e:
            store.append("o1", "U2", [NewEvent("probe.free", payload)])
        assert e.value.path == path
    assert store.head()[0] == 0


GOLDEN_HEAD = (8, "sha256:1a5a2da8967a673eb5163f58b085a3656f628b65dc0492282e8306438eee7c94")
"""The head after :func:`_script` on a store named ``st_contract`` created at START.

It pins the encoding: a change to the record, the canonical form or the genesis changes it.
"""


def test_the_scripted_chain_has_the_pinned_head(backend: Backend, catalogue: Catalogue) -> None:
    clock = FakeClock(START)
    store = backend.open(clock, writer=True, catalogue=catalogue, store_id=SCRIPT_STORE_ID)
    try:
        _script(store, clock)
        assert store.head() == GOLDEN_HEAD
        assert store.verify() is None
    finally:
        store.close()


def test_sqlite_and_postgres_hash_identically(tmp_path: Path, postgres_dsn: str) -> None:
    chains = []
    for backend in (SQLiteBackend(tmp_path), PostgresBackend(postgres_dsn)):
        clock = FakeClock(START)
        store = backend.open(
            clock, writer=True, catalogue=probe_catalogue(), store_id=SCRIPT_STORE_ID
        )
        try:
            _script(store, clock)
            assert store.verify() is None
            chains.append(
                (
                    store.identity(),
                    store.head(),
                    backend.rows("SELECT seq, owner, prev_hash, hash FROM events ORDER BY seq"),
                )
            )
        finally:
            store.close()
    sqlite_chain, postgres_chain = chains
    assert sqlite_chain[0] == postgres_chain[0]
    assert [tuple(r) for r in sqlite_chain[2]] == [tuple(r) for r in postgres_chain[2]]
    assert sqlite_chain[1] == postgres_chain[1] == GOLDEN_HEAD
