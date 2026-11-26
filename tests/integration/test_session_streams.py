"""ADR-P19 D4, D5: a session stream replays from a cursor, marks the tail, then follows."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.app import open_reader, open_session
from aksha.runtime.session import named as n
from aksha.runtime.session.streams import (
    EventItem,
    Heartbeat,
    Resync,
    Synchronized,
    Unknown,
    decode,
    encode,
    subscribe,
)
from aksha.runtime.session.worker import RuntimeThread
from tests.support.session import OWNER, SESSION, SessionBench, config


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[SessionBench]:
    b = SessionBench(tmp_path)
    yield b
    b.close()


def _run(b: SessionBench, session: str = SESSION, cid: str = "c-1") -> None:
    b.session.dispatch(n.SendMessage(owner=OWNER, session=session, text="Write a note",
                                     command_id=cid))  # fmt: skip
    b.session.run_until_yield(OWNER)


def _replay(b: SessionBench, after: int | None = None, **kw: object) -> list[object]:
    out: list[object] = []
    for item in subscribe(b.session.store, OWNER, SESSION, after, heartbeat_s=0.01, **kw):  # type: ignore[arg-type]
        out.append(item)
        if isinstance(item, Synchronized):
            break
    return out


def test_replay_then_one_synchronized_marker_at_the_head(bench: SessionBench) -> None:
    _run(bench)
    items = _replay(bench)
    events = [i for i in items if isinstance(i, EventItem)]
    head = bench.session.store.last_seq(OWNER)
    assert items[-1] == Synchronized(head)
    assert [e.seq for e in events] == sorted(e.seq for e in events)
    assert events[0].event_type == "command.accepted"
    assert "run.ended" in [e.event_type for e in events]


def test_a_cursor_replays_only_what_the_client_missed(bench: SessionBench) -> None:
    _run(bench)
    items = _replay(bench)
    cursor = [i for i in items if isinstance(i, EventItem)][5].seq
    later = [i.seq for i in _replay(bench, cursor) if isinstance(i, EventItem)]
    assert later and min(later) == cursor + 1


def test_another_sessions_events_never_reach_the_stream(bench: SessionBench) -> None:
    _run(bench, "S-0002", "c-2")
    items = _replay(bench)
    assert items == [Synchronized(bench.session.store.last_seq(OWNER))]


def test_a_large_gap_sends_a_resync_instead_of_the_replay(bench: SessionBench) -> None:
    _run(bench)
    items = _replay(bench, max_replay=3)
    head = bench.session.store.last_seq(OWNER)
    assert items == [Resync(head), Synchronized(head)]


def test_live_commits_follow_the_marker_and_quiet_sends_heartbeats(tmp_path: Path) -> None:
    worker = RuntimeThread(lambda: open_session(config(tmp_path)))
    worker.start()
    reader = open_reader(config(tmp_path))
    try:
        stream = subscribe(reader, OWNER, SESSION, 0, heartbeat_s=0.05)
        assert isinstance(next(stream), Synchronized)
        assert isinstance(next(stream), Heartbeat)
        receipt = worker.submit(
            n.SendMessage(owner=OWNER, session=SESSION, text="Write a note", command_id="c-1")
        ).result(10)
        assert receipt.accepted
        seen: list[str] = []
        done = threading.Event()
        for item in stream:
            if isinstance(item, EventItem):
                seen.append(item.event_type)
                if item.event_type == "run.ended":
                    done.set()
                    break
        assert done.is_set() and seen[0] == "command.accepted"
        stream.close()
    finally:
        worker.stop()
        reader.close()


def test_an_unknown_item_decodes_as_unknown_and_keeps_its_cursor(bench: SessionBench) -> None:
    _run(bench)
    items = _replay(bench)
    wire = [encode(i) for i in items]  # type: ignore[arg-type]
    known = frozenset({"command.accepted", "user.message"})
    decoded = [decode(w, known) for w in wire]
    unknown = [d for d in decoded if isinstance(d, Unknown)]
    assert unknown and all(u.seq is not None for u in unknown)
    assert isinstance(decoded[-1], Synchronized)
    assert decode({"kind": "from_a_newer_server", "seq": 7}, known) == Unknown(
        7, "from_a_newer_server"
    )


def test_span_text_never_leaves_through_the_stream() -> None:
    from aksha.runtime.events.envelope import Event
    from aksha.runtime.session.streams import event_item

    span = {"span_id": "EX-0001", "source": "s", "record_id": "r", "version": "1", "start": 0,
            "end": 4, "text": "secret", "tool": "t"}  # fmt: skip
    payload = {"tool": "t", "spans": [span], "shown": []}
    at = "2026-11-27T09:00:00Z"
    event = Event("excerpts.minted", payload, run="R-1", seq=1, owner=OWNER, at=at, boot=1,
                  schema_version=1)  # fmt: skip
    item = event_item(event)
    assert "text" not in item.payload["spans"][0] and item.payload["spans"][0]["span_id"]
