"""Commands with receipts (ADR-P03 D5, D6): a retried command returns the first receipt."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue, EventTypeSpec
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.projections.view import LogView
from aksha.runtime.session.commands import command_key, derive_command_id, dispatch
from aksha.runtime.session.errors import CommandIdReused, CommandNameMismatch
from aksha.runtime.session.types import (
    Command,
    CommandAccepted,
    CommandRefused,
    CommandSpec,
)
from aksha.runtime.storage.ports import EventStore
from tests.support.events import START
from tests.support.steps import types

DECIDED = EventTypeSpec(
    name="decision.recorded",
    schema={"type": "object", "properties": {"approval": {"type": "string"}}},
    writer="tests",
    boundaries=frozenset({"U4"}),
    dedup_key="decide:{approval}",
)
"""A probe decision event with a decide: key: one decision per pending item (D6)."""

NOTED = EventTypeSpec(
    name="message.noted",
    schema={"type": "object", "properties": {"text": {"type": "string"}}},
    writer="tests",
    boundaries=frozenset({"U1"}),
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    catalogue = Catalogue((*CORE_EVENT_TYPES, DECIDED, NOTED))
    s = open_store(tmp_path / "aksha.sqlite", FakeClock(START), writer=True, catalogue=catalogue)
    yield s
    s.close()


class Counting:
    """A command's decide function that counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    def send(self, command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        self.calls += 1
        return CommandAccepted((NewEvent("message.noted", {"text": command.args["text"]}),))

    def approve(self, command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        self.calls += 1
        approval = str(command.args["approval"])
        return CommandAccepted((NewEvent("decision.recorded", {"approval": approval}),))

    def refuse(self, command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        self.calls += 1
        return CommandRefused("hash_mismatch", "the recipient record changed")


def send(text: str = "hi", command_id: str | None = "c-1") -> Command:
    return Command("send", "o", "S-0001", {"text": text}, command_id=command_id)


def test_command_commits_its_receipt_and_its_events_in_one_transaction(
    store: EventStore,
) -> None:
    c = Counting()
    receipt = dispatch(store, send(), CommandSpec("send", "U1", c.send))
    assert (receipt.command_id, receipt.accepted, receipt.duplicate, receipt.refused) == (
        "c-1", True, False, None,
    )  # fmt: skip
    accepted, noted = store.read("o")
    assert (accepted.type, accepted.dedup_key, accepted.seq) == (
        "command.accepted", "cmd:o/c-1", receipt.seq,
    )  # fmt: skip
    assert (accepted.session, accepted.payload["name"]) == ("S-0001", "send")
    assert noted.type == "message.noted"


def test_command_retry_returns_receipt(store: EventStore) -> None:
    c = Counting()
    spec = CommandSpec("send", "U1", c.send)
    first = dispatch(store, send(), spec)
    again = dispatch(store, send(), spec)
    assert again.duplicate and not first.duplicate
    assert (again.seq, again.accepted) == (first.seq, True)
    assert c.calls == 1
    assert types(store) == ["command.accepted", "message.noted"]


def test_command_without_an_id_gets_one_derived_from_what_it_answers(store: EventStore) -> None:
    c = Counting()
    spec = CommandSpec("send", "U1", c.send)
    plain = Command("send", "o", "S-0001", {"text": "hi"}, after_seq=4)
    first = dispatch(store, plain, spec)
    assert first.command_id == derive_command_id(plain)
    assert dispatch(store, plain, spec).duplicate  # an up-arrow retry: the same click
    later = Command("send", "o", "S-0001", {"text": "hi"}, after_seq=9)
    assert not dispatch(store, later, spec).duplicate  # the same words after a new reply
    assert c.calls == 2


def test_derived_id_covers_session_arguments_pending_item_and_last_reply() -> None:
    base = Command("approve", "o", "S-0001", {"x": 1}, answers="A-0001", after_seq=3)
    ids = {
        derive_command_id(base),
        derive_command_id(
            Command("approve", "o", "S-0002", {"x": 1}, answers="A-0001", after_seq=3)
        ),
        derive_command_id(
            Command("approve", "o", "S-0001", {"x": 2}, answers="A-0001", after_seq=3)
        ),
        derive_command_id(
            Command("approve", "o", "S-0001", {"x": 1}, answers="A-0002", after_seq=3)
        ),
        derive_command_id(
            Command("approve", "o", "S-0001", {"x": 1}, answers="A-0001", after_seq=4)
        ),
    }
    assert len(ids) == 5
    assert derive_command_id(base) == derive_command_id(
        Command("approve", "o", "S-0001", {"x": 1}, answers="A-0001", after_seq=3)
    )


def test_two_front_ends_one_decision(store: EventStore) -> None:
    c = Counting()
    spec = CommandSpec("approve", "U4", c.approve)
    web = Command("approve", "o", "S-0001", {"approval": "A-0001"}, command_id="web-1")
    cli = Command("approve", "o", "S-0001", {"approval": "A-0001"}, command_id="cli-1")
    first = dispatch(store, web, spec)
    second = dispatch(store, cli, spec)
    assert first.accepted and first.refused is None
    assert (second.accepted, second.refused, second.duplicate) == (False, "already_decided", False)
    assert types(store) == ["command.accepted", "decision.recorded", "command.refused"]
    refused = list(store.read("o"))[-1]
    assert (refused.dedup_key, refused.payload["code"]) == ("cmd:o/cli-1", "already_decided")
    assert dispatch(store, cli, spec).duplicate  # the refusal is the receipt of cli-1


def test_refused_command_records_its_refusal_once(store: EventStore) -> None:
    c = Counting()
    spec = CommandSpec("send", "U1", c.refuse)
    first = dispatch(store, send(), spec)
    assert (first.accepted, first.refused) == (False, "hash_mismatch")
    again = dispatch(store, send(), spec)
    assert (again.accepted, again.refused, again.duplicate, again.seq) == (
        False, "hash_mismatch", True, first.seq,
    )  # fmt: skip
    assert c.calls == 1
    assert types(store) == ["command.refused"]


def test_one_command_id_for_another_command_is_refused(store: EventStore) -> None:
    spec = CommandSpec("send", "U1", Counting().send)
    dispatch(store, send("hi"), spec)
    with pytest.raises(CommandIdReused) as e:
        dispatch(store, send("something else"), spec)
    assert e.value.code == "command_id_reused"
    assert types(store) == ["command.accepted", "message.noted"]


def test_one_command_id_answering_another_item_is_refused(store: EventStore) -> None:
    spec = CommandSpec("send", "U1", Counting().send)
    first = Command("send", "o", "S-0001", {"text": "hi"}, answers="I-0001", command_id="c-1")
    dispatch(store, first, spec)
    other = Command("send", "o", "S-0001", {"text": "hi"}, answers="I-0002", command_id="c-1")
    with pytest.raises(CommandIdReused):
        dispatch(store, other, spec)
    assert dispatch(store, first, spec).duplicate


def test_command_ids_are_per_owner(store: EventStore) -> None:
    spec = CommandSpec("send", "U1", Counting().send)
    dispatch(store, send(), spec)
    other = Command("send", "p", "S-0001", {"text": "hi"}, command_id="c-1")
    assert not dispatch(store, other, spec).duplicate
    assert command_key("p", "c-1") == "cmd:p/c-1"


def test_spec_must_be_the_commands_own(store: EventStore) -> None:
    with pytest.raises(CommandNameMismatch) as e:
        dispatch(store, send(), CommandSpec("approve", "U4", Counting().approve))
    assert e.value.code == "command_name_mismatch"


def test_the_decision_sees_the_derived_command_id(tmp_path: Path) -> None:
    store = open_store(tmp_path / "a.sqlite", FakeClock(START), writer=True,
                       catalogue=Catalogue((*CORE_EVENT_TYPES, DECIDED, NOTED)))  # fmt: skip
    seen: list[str | None] = []

    def decide(command: Command, view: LogView) -> CommandAccepted | CommandRefused:
        seen.append(command.command_id)
        return CommandAccepted()

    try:
        command = Command("approve", "o", "S-1", {"approval": "A-1"})
        receipt = dispatch(store, command, CommandSpec("approve", "U4", decide))
        assert seen == [receipt.command_id] == [derive_command_id(command)]
    finally:
        store.close()
