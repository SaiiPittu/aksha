"""Tier 3 in the composition root: ``AKSHA_SANDBOX``, ``AKSHA_NETWORK``, image grants.

``AKSHA_SANDBOX`` names a built-in backend (``auto``, ``bwrap``, ``refuse``), which is
probed, or declares an outer box the operator attests (``<name>:<strength>``), which lets a
headless run start but runs no program. The bubblewrap branch depends on the host: each
test asserts what the probe said, so it holds on a host with and without bubblewrap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aksha.adapters.egress.network import SocketResolver
from aksha.adapters.sandbox.bwrap import BwrapSandbox
from aksha.app import RuntimeConfig, execution_tier
from aksha.runtime.sandbox.refuse import RefuseSandbox
from aksha.runtime.session import named as n
from aksha.runtime.storage.errors import StoreError
from tests.support.session import OWNER, SESSION, SessionBench

HOST_HAS_BWRAP = BwrapSandbox().probe().available


def send(b: SessionBench, text: str, **kw: object) -> n.SendMessage:
    return n.SendMessage(owner=OWNER, session=SESSION, text=text, command_id="c-1", **kw)  # type: ignore[arg-type]


def _tier(**env: str):  # type: ignore[no-untyped-def]
    return execution_tier(RuntimeConfig.from_env(env), ())


def test_no_setting_chooses_no_backend_and_keeps_the_network_off() -> None:
    tier = _tier()
    assert tier.box is None and tier.gate_availability is None and tier.network is None


def test_refuse_is_a_backend_that_runs_nothing() -> None:
    tier = _tier(AKSHA_SANDBOX="refuse")
    assert isinstance(tier.box, RefuseSandbox)
    assert tier.gate_availability is not None and not tier.gate_availability.available


@pytest.mark.parametrize("choice", ["bwrap", "auto", "bwrap:os-jail"])
def test_a_built_in_choice_is_probed_never_trusted(choice: str) -> None:
    tier = _tier(AKSHA_SANDBOX=choice)
    got = tier.gate_availability
    assert got is not None and got.available is HOST_HAS_BWRAP
    if HOST_HAS_BWRAP:
        assert isinstance(tier.box, BwrapSandbox) and got.backend == "bwrap"
    else:
        assert isinstance(tier.box, RefuseSandbox) and got.reason
    assert [p.backend for p in tier.probes] == ["bwrap"]


def test_a_declared_outer_box_starts_headless_runs_but_runs_no_program() -> None:
    tier = _tier(AKSHA_SANDBOX="outer:container")
    assert tier.box is None
    got = tier.gate_availability
    assert got is not None and (got.backend, got.strength, got.available) == (
        "outer", "container", True)  # fmt: skip
    assert tier.record()["declared"] is True


@pytest.mark.parametrize("value", ["docker", "bwrap:vm", "outer:none-such", "outer:"])
def test_a_bad_setting_is_refused(value: str) -> None:
    with pytest.raises(StoreError) as refused:
        _tier(AKSHA_SANDBOX=value)
    assert refused.value.code == "sandbox_invalid"


def test_the_network_is_on_only_when_asked() -> None:
    network = _tier(AKSHA_NETWORK="on").network
    assert network is not None and isinstance(network.resolver, SocketResolver)
    with pytest.raises(StoreError) as refused:
        _tier(AKSHA_NETWORK="maybe")
    assert refused.value.code == "network_invalid"


def test_granted_image_keys_are_read_and_recorded() -> None:
    key = "sha256:" + "d" * 64
    tier = _tier(AKSHA_APPROVED_ENVIRONMENTS=key)
    assert tier.approved_images == frozenset({key})
    assert tier.record()["approved_images"] == [key]
    with pytest.raises(StoreError):
        _tier(AKSHA_APPROVED_ENVIRONMENTS="not-a-key")


def test_run_started_records_the_sandbox(tmp_path: Path) -> None:
    b = SessionBench(tmp_path, sandbox="outer:container")
    try:
        assert b.session.dispatch(send(b, "Write a note", headless=True)).accepted
        (started,) = b.payloads("run.started")
        record = started["sandbox"]
        assert (record["backend"], record["declared"], record["network"]) == ("outer", True,
                                                                             "off")  # fmt: skip
    finally:
        b.close()


@pytest.mark.skipif(not HOST_HAS_BWRAP, reason="bwrap does not probe available on this host")
def test_a_headless_run_uses_the_real_backend(tmp_path: Path) -> None:
    b = SessionBench(tmp_path, sandbox="bwrap")
    try:
        assert isinstance(b.session.tier.box, BwrapSandbox)
        assert b.session.dispatch(send(b, "Write a note", headless=True)).accepted
        record = b.payloads("run.started")[0]["sandbox"]
        assert (record["backend"], record["available"]) == ("bwrap", True)
        assert record["version"].startswith("bubblewrap")
    finally:
        b.close()
