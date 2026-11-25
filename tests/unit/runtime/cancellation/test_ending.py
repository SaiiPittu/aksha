"""The stop ending at a stop candidate (ADR-P18 D2): voided interrupts, run.ended, stop.decided."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.cancellation.ending import honour_stop, stop_ending
from aksha.runtime.cancellation.facts import stop_requested
from aksha.runtime.cancellation.types import DEFAULT_REASON
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.projections.view import load_view
from aksha.runtime.storage.ports import EventStore
from tests.support.events import START
from tests.support.steps import recovery_catalogue, started_run, types


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    s = open_store(
        tmp_path / "aksha.sqlite", FakeClock(START), writer=True, catalogue=recovery_catalogue()
    )
    s.append("o", "U1", (started_run("R-0001"),))
    yield s
    s.close()


def _stop(store: EventStore, command_id: str = "c-1") -> None:
    event = stop_requested("R-0001", scope="run", reason=DEFAULT_REASON, by="o",
                           command_id=command_id)  # fmt: skip
    store.append("o", "U4", (event,))


def _raise(store: EventStore, interrupt: str) -> None:
    store.append("o", "U2", (NewEvent("interrupt.raised", {"interrupt": interrupt}, run="R-0001"),))


def test_no_pending_stop_means_no_ending(store: EventStore) -> None:
    assert stop_ending(load_view(store, "o"), "R-0001", "turn_end") == ()
    before = store.last_seq("o")
    assert not honour_stop(store, "o", "R-0001", "turn_end")
    assert store.last_seq("o") == before


def test_ending_voids_open_interrupts_then_ends_the_run_then_decides(store: EventStore) -> None:
    _raise(store, "P-0001")
    _raise(store, "P-0002")
    resolved = NewEvent("interrupt.resolved", {"interrupt": "P-0001"}, run="R-0001")
    store.append("o", "U4", (resolved,))
    _stop(store)
    events = stop_ending(load_view(store, "o"), "R-0001", "turn_end")
    assert [(e.type, dict(e.payload)) for e in events] == [
        ("interrupt.voided", {"interrupt": "P-0002", "reason": "run_stopped"}),
        ("run.ended", {"outcome": "stopped", "reason": "stop_requested"}),
        ("stop.decided", {"walk": 1, "verdict": "stop", "cause": "turn_end",
                          "candidate": "turn_end", "reason": "stop_requested"}),
    ]  # fmt: skip
    assert all(e.run == "R-0001" for e in events)


def test_walk_counts_the_runs_earlier_decisions(store: EventStore) -> None:
    hold = NewEvent("stop.decided", {"walk": 1, "verdict": "hold"}, run="R-0001")
    store.append("o", "U3", (hold,))
    _stop(store)
    *_, decided = stop_ending(load_view(store, "o"), "R-0001", "resume")
    assert decided.payload["walk"] == 2


def test_honour_writes_once_in_one_transaction(store: EventStore) -> None:
    _raise(store, "P-0001")
    _stop(store)
    assert honour_stop(store, "o", "R-0001", "turn_end")
    tail = types(store)[-3:]
    assert tail == ["interrupt.voided", "run.ended", "stop.decided"]
    seqs = [e.seq for e in store.read("o")][-3:]
    assert seqs == list(range(seqs[0], seqs[0] + 3))
    before = store.last_seq("o")
    assert not honour_stop(store, "o", "R-0001", "turn_end")
    assert store.last_seq("o") == before
