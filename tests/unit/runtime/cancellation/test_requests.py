"""Stop requests read from the log (ADR-P18 D1 to D3): pending, honoured, and the runs to end."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.cancellation.ending import honour_stop
from aksha.runtime.cancellation.facts import stop_requested
from aksha.runtime.cancellation.requests import (
    pending_stop,
    runs_to_resume_for_stop,
    stop_before_step,
    stop_requests,
)
from aksha.runtime.cancellation.types import DEFAULT_REASON
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.projections.view import load_view
from aksha.runtime.storage.ports import EventStore
from tests.support.events import START
from tests.support.steps import recovery_catalogue, started_run


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    s = open_store(
        tmp_path / "aksha.sqlite", FakeClock(START), writer=True, catalogue=recovery_catalogue()
    )
    yield s
    s.close()


def _request(store: EventStore, run: str, command_id: str, by: str = "o") -> None:
    event = stop_requested(run, scope="run", reason=DEFAULT_REASON, by=by, command_id=command_id)
    store.append("o", "U4", (event,))


def test_a_run_with_no_request_has_none(store: EventStore) -> None:
    store.append("o", "U1", (started_run("R-0001"),))
    view = load_view(store, "o")
    assert stop_requests(view, "R-0001") == ()
    assert pending_stop(view, "R-0001") is None
    assert not stop_before_step(view, "R-0001")
    assert runs_to_resume_for_stop(view) == ()


def test_requests_are_read_in_order_and_the_newest_is_pending(store: EventStore) -> None:
    store.append("o", "U1", (started_run("R-0001"), started_run("R-0002")))
    _request(store, "R-0001", "c-1", by="person-a")
    _request(store, "R-0001", "c-2", by="person-b")
    view = load_view(store, "o")
    first, second = stop_requests(view, "R-0001")
    assert (first.command_id, first.by, first.reason, first.scope) == (
        "c-1", "person-a", DEFAULT_REASON, "run",
    )  # fmt: skip
    assert first.seq < second.seq
    assert (first.honoured_by, second.honoured_by) == (None, None)
    pending = pending_stop(view, "R-0001")
    assert pending is not None and pending.command_id == "c-2"
    assert stop_before_step(view, "R-0001")
    assert not stop_before_step(view, "R-0002")
    assert runs_to_resume_for_stop(view) == ("R-0001",)


def test_an_honoured_request_names_the_decision_and_is_no_longer_pending(
    store: EventStore,
) -> None:
    store.append("o", "U1", (started_run("R-0001"),))
    _request(store, "R-0001", "c-1")
    _request(store, "R-0001", "c-2")
    assert honour_stop(store, "o", "R-0001", "turn_end")
    view = load_view(store, "o")
    (decided,) = [e for e in view.events if e.type == "stop.decided"]
    assert [r.honoured_by for r in stop_requests(view, "R-0001")] == [decided.seq, decided.seq]
    assert pending_stop(view, "R-0001") is None
    assert not stop_before_step(view, "R-0001")
    assert runs_to_resume_for_stop(view) == ()


def test_a_run_that_ended_otherwise_has_no_pending_stop(store: EventStore) -> None:
    store.append("o", "U1", (started_run("R-0001"),))
    _request(store, "R-0001", "c-1")
    ended = NewEvent("run.ended", {"outcome": "complete", "reason": "done"}, run="R-0001")
    store.append("o", "U3", (ended,))
    view = load_view(store, "o")
    assert pending_stop(view, "R-0001") is None
    (request,) = stop_requests(view, "R-0001")
    assert request.honoured_by is None
