"""Open interrupts read from the log (ADR-P17 D1): resolved or voided closes one."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.interrupts.reducers import pending_interrupts, raised_in
from aksha.runtime.projections.view import load_view
from aksha.runtime.storage.ports import EventStore
from tests.support.events import START
from tests.support.steps import recovery_catalogue, started_run


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    s = open_store(
        tmp_path / "aksha.sqlite", FakeClock(START), writer=True, catalogue=recovery_catalogue()
    )
    s.append("o", "U1", (started_run("R-0001"), started_run("R-0002")))
    for i in ("P-0001", "P-0002", "P-0003"):
        s.append("o", "U2", (NewEvent("interrupt.raised", {"interrupt": i}, run="R-0001"),))
    s.append("o", "U2", (NewEvent("interrupt.raised", {"interrupt": "P-0004"}, run="R-0002"),))
    yield s
    s.close()


def test_resolved_and_voided_interrupts_are_closed(store: EventStore) -> None:
    resolved = NewEvent("interrupt.resolved", {"interrupt": "P-0001"}, run="R-0001")
    store.append("o", "U4", (resolved,))
    voided = NewEvent("interrupt.voided", {"interrupt": "P-0003", "reason": "run_stopped"},
                      run="R-0001")  # fmt: skip
    store.append("o", "U3", (voided,))
    view = load_view(store, "o")
    assert pending_interrupts(view, "R-0001") == ("P-0002",)
    assert pending_interrupts(view, "R-0002") == ("P-0004",)


def test_raised_in_lists_every_interrupt_of_the_runs(store: EventStore) -> None:
    view = load_view(store, "o")
    assert raised_in(view, frozenset({"R-0001"})) == {"P-0001", "P-0002", "P-0003"}
    assert raised_in(view, frozenset()) == frozenset()
