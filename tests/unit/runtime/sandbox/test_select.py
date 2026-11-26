"""``select_backend`` (ADR-P12 decision 2): probes once, never falls back to a weaker box."""

from __future__ import annotations

import pytest

from aksha.runtime.sandbox.errors import SandboxRefused
from aksha.runtime.sandbox.refuse import RefuseSandbox
from aksha.runtime.sandbox.select import select_backend
from aksha.runtime.sandbox.types import BoxRequest, ExecPolicy
from tests.support.sandbox import FakeSandbox


def test_auto_takes_the_strongest_available_backend() -> None:
    jail, vm = FakeSandbox("jail", "os-jail"), FakeSandbox("vm", "vm")
    box, probes = select_backend("auto", {"jail": lambda: jail, "vm": lambda: vm})
    assert box is vm and [p.backend for p in probes] == ["jail", "vm"]


def test_a_named_backend_that_is_unavailable_becomes_refuse_not_another_backend() -> None:
    off, other = FakeSandbox("off", available=False), FakeSandbox("other")
    box, probes = select_backend("off", {"off": lambda: off, "other": lambda: other})
    assert isinstance(box, RefuseSandbox) and not box.probe().available
    assert "switched off" in box.probe().reason and [p.backend for p in probes] == ["off"]


def test_auto_with_nothing_available_is_refuse() -> None:
    box, _ = select_backend("auto", {"off": lambda: FakeSandbox("off", available=False)})
    assert isinstance(box, RefuseSandbox)


def test_refuse_is_always_there_and_runs_nothing() -> None:
    box, probes = select_backend("refuse", {})
    assert not probes[0].available
    with pytest.raises(SandboxRefused) as refused:
        box.run(BoxRequest("x", ("true",), ExecPolicy()), FakeBlobs(), Holder())
    assert refused.value.code == "sandbox_unavailable"


def test_an_unknown_choice_is_refused() -> None:
    with pytest.raises(SandboxRefused) as refused:
        select_backend("docker", {})
    assert refused.value.code == "sandbox_invalid"


class FakeBlobs:
    def get(self, ref: str) -> bytes:
        raise KeyError(ref)


class Holder:
    def attach(self, unit: object) -> None:
        pass

    def detach(self) -> None:
        pass
