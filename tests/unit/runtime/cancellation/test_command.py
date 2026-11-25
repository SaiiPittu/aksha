"""The stop command (ADR-P18 D1): committed, not done; idempotent; refused on an ended run."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.cancellation.command import (
    NOT_FOUND,
    RUN_NOT_OPEN,
    SCOPE_NOT_BUILT,
    stop,
)
from aksha.runtime.cancellation.types import DEFAULT_REASON
from aksha.runtime.events.envelope import NewEvent
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


def _requests(store: EventStore, owner: str = "o") -> list[dict[str, object]]:
    return [dict(e.payload) for e in store.read(owner) if e.type == "run.stop_requested"]


def test_stop_commits_a_request_and_its_receipt_together(store: EventStore) -> None:
    receipt = stop(store, "o", "R-0001", command_id="cli-7f3a")
    assert (receipt.accepted, receipt.duplicate, receipt.command_id) == (True, False, "cli-7f3a")
    assert types(store)[-2:] == ["command.accepted", "run.stop_requested"]
    assert _requests(store) == [
        {"scope": "run", "reason": DEFAULT_REASON, "by": "o", "command_id": "cli-7f3a"}
    ]
    (request,) = [e for e in store.read("o") if e.type == "run.stop_requested"]
    assert request.run == "R-0001"
    assert not any(e.type == "run.ended" for e in store.read("o"))  # committed, not stopped


def test_stop_records_the_reason_and_who_asked(store: EventStore) -> None:
    stop(store, "o", "R-0001", reason="wrong file", by="web:person-1", command_id="c-1")
    assert _requests(store) == [
        {"scope": "run", "reason": "wrong file", "by": "web:person-1", "command_id": "c-1"}
    ]


def test_stop_is_idempotent(store: EventStore) -> None:
    first = stop(store, "o", "R-0001", command_id="c-1")
    again = stop(store, "o", "R-0001", command_id="c-1")
    assert (again.duplicate, again.seq) == (True, first.seq)
    assert len(_requests(store)) == 1
    second = stop(store, "o", "R-0001", command_id="c-2")
    assert second.accepted and not second.duplicate
    assert [r["command_id"] for r in _requests(store)] == ["c-1", "c-2"]


def test_a_derived_command_id_makes_a_retried_click_one_request(store: EventStore) -> None:
    first = stop(store, "o", "R-0001")
    again = stop(store, "o", "R-0001")
    assert first.command_id.startswith("d-")
    assert again.duplicate and again.command_id == first.command_id
    assert len(_requests(store)) == 1


def test_stop_on_ended_run_refused(store: EventStore) -> None:
    ended = NewEvent("run.ended", {"outcome": "complete", "reason": "done"}, run="R-0001")
    store.append("o", "U3", (ended,))
    before = store.last_seq("o")
    receipt = stop(store, "o", "R-0001", command_id="c-1")
    assert (receipt.accepted, receipt.refused) == (False, RUN_NOT_OPEN)
    assert types(store)[before:] == ["command.refused"]  # the receipt, and no stop request
    assert _requests(store) == []


def test_stop_of_another_owners_run_is_not_found(store: EventStore) -> None:
    receipt = stop(store, "other", "R-0001", command_id="c-1")
    assert receipt.refused == NOT_FOUND
    assert _requests(store, "other") == [] and _requests(store) == []


def test_turn_scope_is_designed_only(store: EventStore) -> None:
    receipt = stop(store, "o", "R-0001", scope="turn", command_id="c-1")
    assert receipt.refused == SCOPE_NOT_BUILT
    assert _requests(store) == []
