"""A send through a command, the outbox and a ledgered system, crashed at each point (ADR-P03).

Runs against both storage adapters. The ``handoff`` command commits its receipt and the
outbox entry in one transaction; the worker runs the entry after that commit; a restart
reconciles by key. Every scenario ends with exactly one ledger row.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue
from aksha.runtime.execution.types import StepPolicy
from aksha.runtime.outbox.errors import OutboxRefused
from aksha.runtime.outbox.outbox import enqueue, entries
from aksha.runtime.outbox.types import EffectSpec, OutboxEntry
from aksha.runtime.outbox.worker import OutboxWorker
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.session.commands import dispatch
from aksha.runtime.session.types import Command, CommandAccepted, CommandRefused, CommandSpec
from aksha.runtime.storage.ports import EventStore
from tests.support.backends import Backend, PostgresBackend, SQLiteBackend
from tests.support.events import START
from tests.support.steps import Crash
from tests.support.systems import Ledger

SEND = EffectSpec(system="mailer", key_fields=("/document", "/to"), semantics="reconcile")
PACKET = {"document": "packet/P1@v2", "to": "r-7"}


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
        self.store: EventStore = backend.open(self.clock, writer=True, catalogue=_catalogue())
        self.revoked: set[str] = set()
        self.spec = CommandSpec("handoff", "U5", self.decide_handoff)

    def authority(self, entry: OutboxEntry, view: LogView) -> str | None:
        """Stands in for the approval and invalidation checks (P17, P05): a revoked recipient."""
        return "hash_mismatch" if entry.request.get("to") in self.revoked else None

    def decide_handoff(self, command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        try:
            out = enqueue(
                view, "send", command.args, SEND, session=command.session,
                authority=self.authority,
            )  # fmt: skip
        except OutboxRefused as e:
            return CommandRefused(e.code, e.message)
        return CommandAccepted(() if out.event is None else (out.event,))

    def restart(self) -> None:
        self.store.close()
        self.store = self.backend.open(self.clock, writer=True, catalogue=_catalogue())

    def worker(self) -> OutboxWorker:
        policies = {"send": StepPolicy()}
        return OutboxWorker(self.store, "o", self.clock, {"mailer": self.ledger},
                            policies=policies, authority=self.authority)  # fmt: skip

    def handoff(self, command_id: str, args: dict[str, Any] = PACKET, owner: str = "o") -> Any:
        return dispatch(self.store, Command("handoff", owner, "S-0001", args,
                                            command_id=command_id), self.spec)  # fmt: skip

    def close(self) -> None:
        self.store.close()


def _catalogue() -> Catalogue:
    return Catalogue(CORE_EVENT_TYPES)


@pytest.fixture
def world(backend: Backend) -> Iterator[World]:
    w = World(backend)
    yield w
    w.close()


def test_handoff_commits_receipt_and_entry_then_the_worker_sends(world: World) -> None:
    receipt = world.handoff("c-1")
    assert receipt.accepted
    assert [e.type for e in world.store.read("o")] == ["command.accepted", "outbox.enqueued"]
    assert world.ledger.applied == []  # the acknowledgement means committed, not done
    (done,) = world.worker().drain()
    assert done.state == "completed"
    assert len(world.ledger.rows) == 1


def test_send_retried_3x_restart_1x_one_row(world: World) -> None:
    for _ in range(3):
        assert world.handoff("c-1").accepted
        world.worker().drain()
    world.restart()
    world.worker().recover()
    assert world.handoff("c-1").duplicate
    world.worker().drain()
    assert len(world.ledger.rows) == 1
    assert len(world.ledger.applied) == 1


def test_crash_after_send_intent_sends_once_after_restart(world: World) -> None:
    world.handoff("c-1")  # committed; the process dies before the worker runs
    world.restart()
    assert world.worker().reconcile("landed") == ()  # nothing was claimed, nothing to ask
    (done,) = world.worker().reconcile("unlanded")  # D7: it did not land, so recovery sends
    assert done.state == "completed"
    assert world.worker().drain() == ()
    assert len(world.ledger.applied) == 1


def test_an_unclaimed_entry_is_rechecked_before_recovery_sends_it(world: World) -> None:
    world.handoff("c-1")
    world.restart()
    world.revoked.add("r-7")  # the approval no longer covers the send
    (done,) = world.worker().recover()
    assert (done.state, done.reason) == ("abandoned", "hash_mismatch")
    assert world.ledger.applied == []


def test_crash_after_send_before_outcome_records_the_receipt_from_lookup(world: World) -> None:
    world.handoff("c-1")
    world.ledger.crash_after_apply = True
    with pytest.raises(Crash):
        world.worker().drain()
    world.restart()
    world.ledger.crash_after_apply = False
    (done,) = world.worker().recover()
    assert (done.state, done.receipt) == ("completed", {"message_id": "m-1"})
    assert len(world.ledger.applied) == 1
    assert world.worker().drain() == ()


def test_recipient_change_after_intent_is_abandoned_hash_mismatch(world: World) -> None:
    world.handoff("c-1")
    world.ledger.crash_before_apply = True
    with pytest.raises(Crash):
        world.worker().drain()
    world.restart()
    world.ledger.crash_before_apply = False
    world.revoked.add("r-7")  # the recipient record changed before the restart finished it
    (done,) = world.worker().recover()
    assert (done.state, done.reason) == ("abandoned", "hash_mismatch")
    assert world.ledger.rows == {}


def test_rebuild_run_send_returns_existing_receipt(world: World) -> None:
    world.handoff("c-1")
    world.worker().drain()
    # A rebuild run regenerates an equal packet version and hands it off under a new ID.
    rebuilt = world.handoff("c-9", dict(PACKET))
    assert rebuilt.accepted and not rebuilt.duplicate
    assert world.worker().drain() == ()
    (entry,) = entries(load_view(world.store, "o")).values()
    assert (entry.state, entry.receipt) == ("completed", {"message_id": "m-1"})
    assert len(world.ledger.applied) == 1


def test_effect_key_includes_owner(world: World) -> None:
    world.handoff("c-1", owner="o")
    world.handoff("c-1", owner="p")
    world.worker().drain()
    OutboxWorker(world.store, "p", world.clock, {"mailer": world.ledger}).drain()
    assert len(world.ledger.rows) == 2


def test_send_refused_by_authority_writes_no_entry(world: World) -> None:
    world.revoked.add("r-7")
    receipt = world.handoff("c-1")
    assert (receipt.accepted, receipt.refused) == (False, "hash_mismatch")
    assert "outbox.enqueued" not in [e.type for e in world.store.read("o")]
    assert world.worker().drain() == ()
    assert world.ledger.applied == []
