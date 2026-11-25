"""The kill boundary (ADR-P18 D6): ask to settle, wait a bounded grace, then terminate."""

from __future__ import annotations

import pytest

from aksha.runtime.cancellation.kill import kill_boundary
from aksha.runtime.cancellation.ports import KillableUnit
from aksha.runtime.cancellation.types import KillPolicy


class FakeUnit:
    """A unit that settles on request after ``settles_after`` waits, or never."""

    def __init__(self, *, obeys: bool) -> None:
        self.obeys = obeys
        self.calls: list[str] = []
        self.waits: list[float] = []
        self._settled = False

    def request_settle(self) -> None:
        self.calls.append("settle")
        if self.obeys:
            self._settled = True

    def terminate(self) -> None:
        self.calls.append("terminate")
        self._settled = True

    def settled(self) -> bool:
        return self._settled

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        return self._settled


def test_a_unit_that_settles_is_never_terminated() -> None:
    unit = FakeUnit(obeys=True)
    assert kill_boundary(unit, KillPolicy(grace_seconds=2.0)) == "settled"
    assert unit.calls == ["settle"]
    assert unit.waits == [2.0]


def test_a_unit_that_ignores_the_request_is_terminated_after_the_grace() -> None:
    unit = FakeUnit(obeys=False)
    policy = KillPolicy(grace_seconds=0.5, reap_seconds=0.25)
    assert kill_boundary(unit, policy) == "killed"
    assert unit.calls == ["settle", "terminate"]
    assert unit.waits == [0.5, 0.25]


def test_an_already_settled_unit_is_left_alone() -> None:
    unit = FakeUnit(obeys=True)
    unit.request_settle()
    unit.calls.clear()
    assert kill_boundary(unit, KillPolicy()) == "settled"
    assert unit.calls == []


def test_the_fake_fits_the_port() -> None:
    unit: KillableUnit = FakeUnit(obeys=True)
    assert not unit.settled()


@pytest.mark.parametrize("field", ["grace_seconds", "reap_seconds"])
def test_negative_waits_are_refused(field: str) -> None:
    with pytest.raises(ValueError):
        KillPolicy(**{field: -1.0})


def test_the_grace_is_the_placeholder() -> None:
    assert KillPolicy().grace_seconds == 5.0
