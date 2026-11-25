"""The stop's holds (ADR-P18 D2, D4) and the TC-4 audit that no automatic start beats them."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.cancellation.ending import honour_stop
from aksha.runtime.cancellation.facts import stop_requested
from aksha.runtime.cancellation.hold import held_auto_starts, standing_hold, stop_hold
from aksha.runtime.cancellation.types import DEFAULT_REASON, STOP_REQUESTED, STOPPED_BY_PERSON
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution import facts
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


def _stop(store: EventStore, run: str, command_id: str = "c-1") -> None:
    event = stop_requested(run, scope="run", reason=DEFAULT_REASON, by="o", command_id=command_id)
    store.append("o", "U4", (event,))


def _stopped(store: EventStore, run: str, agent: str = "a") -> None:
    store.append("o", "U1", (started_run(run, agent=agent),))
    _stop(store, run, command_id=f"c-{run}")
    assert honour_stop(store, "o", run, "turn_end")


def test_a_pending_request_holds_its_run(store: EventStore) -> None:
    store.append("o", "U1", (started_run("R-0001"), started_run("R-0002")))
    _stop(store, "R-0001")
    view = load_view(store, "o")
    hold = stop_hold(view, run="R-0001")
    assert hold is not None and (hold.reason, hold.scope) == (STOP_REQUESTED, "R-0001")
    assert stop_hold(view, run="R-0002") is None
    assert stop_hold(view, agent="a") is None  # only a stopped run holds its agent


def test_a_stopped_run_holds_its_agent_and_not_another(store: EventStore) -> None:
    _stopped(store, "R-0001", agent="a")
    store.append("o", "U1", (started_run("R-0002", agent="b"),))
    view = load_view(store, "o")
    request = standing_hold(view, "a")
    assert request is not None and request.run == "R-0001"
    hold = stop_hold(view, agent="a")
    assert hold is not None and (hold.reason, hold.scope) == (STOPPED_BY_PERSON, "a")
    assert standing_hold(view, "b") is None
    assert stop_hold(view, run="R-0001") is None  # the run itself has ended


def test_a_new_run_the_person_asks_for_clears_the_standing_hold(store: EventStore) -> None:
    _stopped(store, "R-0001")
    store.append("o", "U1", (started_run("R-0002"),))  # cause request: a person's command
    assert standing_hold(load_view(store, "o"), "a") is None


def test_a_rebuild_start_does_not_clear_it(store: EventStore) -> None:
    _stopped(store, "R-0001")
    rebuild = NewEvent("run.started", {"agent": "a", "cause": "rebuild"}, run="R-0002")
    store.append("o", "U9", (rebuild,))
    assert standing_hold(load_view(store, "o"), "a") is not None


def test_an_answer_to_the_agents_interrupt_clears_it(store: EventStore) -> None:
    store.append("o", "U1", (started_run("R-0002"),))
    store.append("o", "U2", (NewEvent("interrupt.raised", {"interrupt": "P-0001"}, run="R-0002"),))
    _stopped(store, "R-0001")
    answer = NewEvent(
        "command.accepted",
        {"command_id": "c-ans", "name": "answer", "args_hash": "sha256:" + "0" * 64,
         "answers": "P-0001"},
    )  # fmt: skip
    store.append("o", "U4", (answer,))
    assert standing_hold(load_view(store, "o"), "a") is None


def test_the_audit_passes_a_log_that_respects_the_holds(store: EventStore) -> None:
    store.append("o", "U1", (started_run("R-0002"),))
    _stopped(store, "R-0001")
    store.append("o", "U1", (started_run("R-0003"),))
    store.append("o", "U3", (facts.resume_record("R-0002", 1, verdict="continue"),))
    assert held_auto_starts(load_view(store, "o")) == ()


def test_the_audit_catches_a_resume_while_the_agent_is_held(store: EventStore) -> None:
    store.append("o", "U1", (started_run("R-0002"),))
    _stopped(store, "R-0001")
    store.append("o", "U3", (facts.resume_record("R-0002", 1, verdict="continue"),))
    (found,) = held_auto_starts(load_view(store, "o"))
    assert (found.type, found.run, found.reason) == ("stop.decided", "R-0002", STOPPED_BY_PERSON)


def test_the_audit_catches_a_rebuild_start_while_the_agent_is_held(store: EventStore) -> None:
    _stopped(store, "R-0001")
    rebuild = NewEvent("run.started", {"agent": "a", "cause": "rebuild"}, run="R-0002")
    store.append("o", "U9", (rebuild,))
    (found,) = held_auto_starts(load_view(store, "o"))
    assert (found.type, found.run, found.reason) == ("run.started", "R-0002", STOPPED_BY_PERSON)


def test_the_audit_catches_a_step_started_after_a_stop_request(store: EventStore) -> None:
    store.append("o", "U1", (started_run("R-0001"),))
    _stop(store, "R-0001")
    started = NewEvent(
        "step.started", {"kind": "model", "name": "m", "counted": True},
        run="R-0001", instance="a", step="R-0001/a/t01",
    )  # fmt: skip
    store.append("o", "U2a", (started,))
    (found,) = held_auto_starts(load_view(store, "o"))
    assert (found.type, found.run, found.reason) == ("step.started", "R-0001", STOP_REQUESTED)


def test_a_stop_verdict_is_not_a_start(store: EventStore) -> None:
    _stopped(store, "R-0001")
    assert held_auto_starts(load_view(store, "o")) == ()
