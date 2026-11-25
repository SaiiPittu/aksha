import dataclasses

import pytest

from aksha.runtime.events.envelope import Event, NewEvent


def test_new_event_defaults_to_no_context() -> None:
    e = NewEvent("probe.noted", {"n": 1})
    assert (e.run, e.instance, e.step, e.session, e.dedup_key) == (None, None, None, None, None)


def test_event_adds_the_store_filled_fields() -> None:
    e = Event(
        type="probe.noted",
        payload={"n": 1},
        run="R",
        seq=4,
        owner="o1",
        at="2026-11-27T09:00:00.000000Z",
        boot=1,
        schema_version=1,
    )
    assert isinstance(e, NewEvent)
    assert (e.seq, e.owner, e.boot) == (4, "o1", 1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.seq = 5  # type: ignore[misc]


def test_envelope_context_names_the_template_fields() -> None:
    e = Event(
        type="t.x", payload={}, run="R", step="s1", seq=1, owner="o", at="", boot=1,
        schema_version=1,
    )  # fmt: skip
    assert e.context() == {"run": "R", "instance": None, "step": "s1", "session": None}
