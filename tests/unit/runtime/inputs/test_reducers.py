"""Pending input is a projection (ADR-P27 D2): queued until a consumed fact names it."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.inputs.reducers import consumed_at, pending_input, queued_inputs
from aksha.runtime.projections.view import load_view
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.tasks.types import InstanceRef
from tests.support.events import START
from tests.support.steps import recovery_catalogue, started_run


def queued(ident: str, lane: str, text: str, instance: str | None = None) -> NewEvent:
    payload = {"input_id": ident, "lane": lane, "text": text, "instance": instance,
               "by": "person", "command_id": f"c-{ident}"}  # fmt: skip
    return NewEvent("input.queued", payload, run="R-0001", session="S-0001")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    s = open_store(
        tmp_path / "aksha.sqlite", FakeClock(START), writer=True, catalogue=recovery_catalogue()
    )
    s.append("o", "U1", (started_run("R-0001"),))
    yield s
    s.close()


def test_queued_input_is_pending_until_consumed(store: EventStore) -> None:
    store.append("o", "U4", (queued("IN-0001", "steer", "use the helper"),))
    store.append("o", "U4", (queued("IN-0002", "follow_up", "and a summary", "write"),))
    store.append("o", "U4", (queued("IN-0003", "steer", "other task", "read"),))
    view = load_view(store, "o")
    ref = InstanceRef.parse("R-0001", "write")
    assert [i.input_id for i in queued_inputs(view, "R-0001")] == ["IN-0001", "IN-0002", "IN-0003"]
    assert [i.input_id for i in pending_input(view, ref)] == ["IN-0001", "IN-0002"]
    assert [i.input_id for i in pending_input(view, ref, lane="steer")] == ["IN-0001"]
    consumed = NewEvent("input.consumed", {"input_id": "IN-0001", "by": "context",
                                           "step_id": "R-0001/write/t1"},
                        run="R-0001", instance="write", step="R-0001/write/t1")  # fmt: skip
    store.append("o", "U2", (consumed,))
    view = load_view(store, "o")
    assert [i.input_id for i in pending_input(view, ref)] == ["IN-0002"]
    (at,) = consumed_at(view, ref, "R-0001/write/t1")
    assert (at.input_id, at.text, at.lane) == ("IN-0001", "use the helper", "steer")


def test_an_amendment_is_never_pending(store: EventStore) -> None:
    store.append("o", "U4", (queued("IN-0001", "amend", "include spreadsheets"),))
    view = load_view(store, "o")
    assert pending_input(view, InstanceRef.parse("R-0001", "write")) == ()
