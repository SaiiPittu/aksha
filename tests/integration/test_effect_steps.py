"""Effectful tool steps through the outbox, crashed at each commit point (ADR-P03 D1, D7).

Runs against both storage adapters. An effectful step commits ``step.started`` with its
``outbox.enqueued`` (U2a); the worker claims the entry (U6) and commits
``outbox.completed`` with the step's ``step.completed`` (U2). After a crash, recovery on
startup finishes the entry in ADR-P02 D5's order and the step's result comes with it.
Every scenario ends with exactly one ledger row, and the step's result once.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.runtime.cancellation.command import stop
from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue
from aksha.runtime.execution.errors import EffectNeedsOutbox
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.recovery import recover_on_startup, recover_owner
from aksha.runtime.execution.types import StepId, StepPolicy
from aksha.runtime.outbox.outbox import entries
from aksha.runtime.outbox.types import EffectSpec, OutboxEntry
from aksha.runtime.outbox.worker import OutboxWorker
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.storage.ports import EventStore
from tests.support.backends import Backend, PostgresBackend, SQLiteBackend
from tests.support.events import START
from tests.support.steps import Crash, FaultStore, Script, started_run
from tests.support.systems import Ledger, NoLedger

H1 = "sha256:" + "1" * 64
LOADED = {"a": H1}
FILE = EffectSpec(system="mailer", key_fields=("/document", "/to"), semantics="reconcile")
HOOK = EffectSpec(system="hook", key_fields=("/event",), semantics="at_most_once",
                  on_unknown="interrupt")  # fmt: skip
PACKET = {"document": "packet/P1@v2", "to": "r-7"}
S1 = StepId("R-0001", "a", turn=1, call=1)
S2 = StepId("R-0001", "a", turn=1, call=2)
S3 = StepId("R-0001", "a", turn=1, call=3)


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Backend]:
    if request.param == "sqlite":
        yield SQLiteBackend(tmp_path)
        return
    yield PostgresBackend(request.getfixturevalue("postgres_dsn"))


class World:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.clock = FakeClock(START)
        self.ledger = Ledger("mailer")
        self.hook = NoLedger("hook")
        self.revoked: set[str] = set()
        self.store: EventStore = self._open()
        self.store.append("o", "U1", (started_run("R-0001", definition_hash=H1),))

    def _open(self) -> EventStore:
        return self.backend.open(self.clock, writer=True, catalogue=Catalogue(CORE_EVENT_TYPES))

    def authority(self, entry: OutboxEntry, view: LogView) -> str | None:
        return "hash_mismatch" if entry.request.get("to") in self.revoked else None

    def worker(self, store: Any = None) -> OutboxWorker:
        systems = {"mailer": self.ledger, "hook": self.hook}
        return OutboxWorker(store or self.store, "o", self.clock, systems,  # type: ignore[arg-type]
                            policies={"file": StepPolicy()}, authority=self.authority)  # fmt: skip

    def steps(self, store: Any = None) -> StepExecutor:
        s = store or self.store
        return StepExecutor(s, "o", self.clock, outbox=self.worker(s))

    def restart(self) -> None:
        self.store.close()
        self.store = self._open()

    def log(self) -> list[tuple[str, str | None]]:
        return [(e.type, e.step) for e in self.store.read("o")]

    def results(self) -> list[dict[str, Any]]:
        return [dict(e.payload) for e in self.store.read("o") if e.type == "step.completed"]


@pytest.fixture
def world(backend: Backend) -> Iterator[World]:
    w = World(backend)
    yield w
    w.store.close()


def test_intent_and_entry_commit_together_and_the_result_with_the_receipt(world: World) -> None:
    faults = FaultStore(world.store)
    result = world.steps(faults).effect_step(S1, "file", PACKET, FILE)
    assert faults.calls == [
        ("U2a", ("step.started", "outbox.enqueued")),
        ("U6", ("outbox.claimed",)),
        ("U2", ("outbox.completed", "step.completed")),
    ]
    assert result.outcome == "ok" and not result.replayed
    assert result.value is not None and result.value["receipt"] == {"message_id": "m-1"}
    (entry,) = entries(load_view(world.store, "o")).values()
    assert (entry.state, entry.step, entry.run) == ("completed", str(S1), "R-0001")
    again = world.steps().effect_step(S1, "file", PACKET, FILE)
    assert again.replayed and again.value == result.value
    assert len(world.ledger.applied) == 1


@pytest.mark.parametrize(
    "point",
    ["before_claim", "after_claim", "after_apply"],
)
def test_crash_at_each_commit_point_sends_once_and_records_once(world: World, point: str) -> None:
    def die_on(boundary: str, kinds: tuple[str, ...]) -> bool:
        return (point == "before_claim" and kinds == ("outbox.claimed",)) or (
            point == "after_apply" and "outbox.completed" in kinds
        )

    world.ledger.crash_before_apply = point == "after_claim"
    with pytest.raises(Crash):
        world.steps(FaultStore(world.store, die_on)).effect_step(S1, "file", PACKET, FILE)
    world.ledger.crash_before_apply = False
    world.restart()
    report = recover_on_startup(world.store, LOADED, outbox=lambda o: world.worker())
    (key,) = entries(load_view(world.store, "o"))
    assert report.reconciled == (("o", key),)
    assert report.in_flight == {"o": ("R-0001",)}
    (done,) = world.results()
    assert done["outcome"] == "ok" and done["value"]["receipt"] == {"message_id": "m-1"}
    assert done.get("reconciled", False) == (point == "after_apply")  # asked, not re-applied
    resumed = world.steps().effect_step(S1, "file", PACKET, FILE)  # the run's code again
    assert resumed.replayed
    assert len(world.ledger.rows) == 1
    # One row; the only second call is the one that died before it applied anything.
    assert len(world.ledger.applied) == (2 if point == "after_claim" else 1)


def test_a_dangling_effect_step_is_finished_through_its_entry(world: World) -> None:
    with pytest.raises(Crash):
        world.steps(FaultStore(world.store, lambda b, k: k == ("outbox.claimed",))).effect_step(
            S1, "file", PACKET, FILE
        )
    world.restart()
    result = world.steps().effect_step(S1, "file", PACKET, FILE)  # no recovery ran first
    assert result.outcome == "ok" and not result.replayed
    assert len(world.ledger.applied) == 1


def test_an_at_most_once_effect_dispatched_before_a_crash_is_never_sent_again(
    world: World,
) -> None:
    world.hook.crash_after_apply = True
    with pytest.raises(Crash):
        world.steps().effect_step(S1, "hook", {"event": "e-1"}, HOOK)
    world.hook.crash_after_apply = False
    world.restart()
    recover_on_startup(world.store, LOADED, outbox=lambda o: world.worker())
    (done,) = world.results()
    assert (done["outcome"], done["code"], done["on_unknown"]) == (
        "interrupted", "outcome_unknown", "interrupt",
    )  # fmt: skip
    assert world.steps().effect_step(S1, "hook", {"event": "e-1"}, HOOK).replayed
    assert world.hook.fired == [world.hook.fired[0]]


def test_a_refused_effect_enqueues_nothing(world: World) -> None:
    world.revoked.add("r-7")
    result = world.steps().effect_step(S1, "file", PACKET, FILE)
    assert (result.outcome, result.code) == ("error", "hash_mismatch")
    assert [t for t, _ in world.log()] == ["run.started", "step.started", "step.completed"]
    assert world.ledger.applied == []


def test_an_equal_request_in_another_step_reaches_the_first_receipt(world: World) -> None:
    first = world.steps().effect_step(S1, "file", PACKET, FILE)
    second = world.steps().effect_step(S2, "file", dict(PACKET), FILE)
    assert second.outcome == "ok" and second.value == first.value
    assert len(world.ledger.applied) == 1
    assert [t for t, _ in world.log()].count("outbox.enqueued") == 1


def test_a_stopped_runs_unclaimed_effect_is_abandoned_not_sent(world: World) -> None:
    with pytest.raises(Crash):
        world.steps(FaultStore(world.store, lambda b, k: k == ("outbox.claimed",))).effect_step(
            S1, "file", PACKET, FILE
        )
    world.restart()
    stop(world.store, "o", "R-0001", command_id="c-1")
    report = recover_on_startup(world.store, LOADED, outbox=lambda o: world.worker())
    assert report.ended == (("o", "R-0001", "stop_requested"),)
    (done,) = world.results()
    assert (done["outcome"], done["code"]) == ("cancelled", "run_stopped")
    (entry,) = entries(load_view(world.store, "o")).values()
    assert (entry.state, entry.reason) == ("abandoned", "run_stopped")
    assert world.ledger.applied == []


def test_effects_need_an_outbox_worker(world: World) -> None:
    with pytest.raises(EffectNeedsOutbox):
        StepExecutor(world.store, "o", world.clock).effect_step(S1, "file", PACKET, FILE)
    with pytest.raises(Crash):
        world.steps(FaultStore(world.store, lambda b, k: k == ("outbox.claimed",))).effect_step(
            S1, "file", PACKET, FILE
        )
    with pytest.raises(EffectNeedsOutbox):
        recover_owner(world.store, "o", LOADED)  # entries to finish, no worker: refused
    assert world.ledger.applied == []


def test_recovery_runs_the_outbox_phases_before_closing_steps(world: World) -> None:
    """ADR-P02 D5: landed, (source check), unlanded, then at_most_once steps, then classify."""
    world.ledger.crash_after_apply = True  # S1's entry is claimed and landed
    with pytest.raises(Crash):
        world.steps().effect_step(S1, "file", PACKET, FILE)
    world.ledger.crash_after_apply = False
    with pytest.raises(Crash):  # S2's entry is enqueued and never claimed
        world.steps(FaultStore(world.store, lambda b, k: k == ("outbox.claimed",))).effect_step(
            S2, "file", {**PACKET, "to": "r-8"}, FILE
        )
    with pytest.raises(Crash):  # S3 is a plain at_most_once step, dispatched
        world.steps().step(S3, "tool", "send", "sha256:" + "0" * 64, Script().crash("send"),
                           StepPolicy(semantics="at_most_once"))  # fmt: skip
    world.restart()
    faults = FaultStore(world.store)
    recovery = recover_owner(cast(EventStore, faults), "o", LOADED, outbox=world.worker(faults))
    assert faults.calls == [
        ("U2", ("outbox.completed", "step.completed")),  # landed: S1's receipt, asked by key
        ("U6", ("outbox.claimed",)),  # unlanded: S2 runs under its key
        ("U2", ("outbox.completed", "step.completed")),
        ("U2", ("step.completed",)),  # then S3 is closed outcome_unknown
    ]
    assert recovery.closed_unknown == (str(S3),)
    assert len(recovery.reconciled) == 2
    assert recovery.in_flight == ("R-0001",)  # classified last
    assert len(world.ledger.rows) == 2
