"""Tier 3 in the composition root: ``AKSHA_SANDBOX``, ``AKSHA_NETWORK``, image grants.

``AKSHA_SANDBOX`` names a built-in backend (``auto``, ``bwrap``, ``refuse``), which is
probed, or declares an outer box the operator attests (``<name>:<strength>``), which lets a
headless run start but runs no program. The bubblewrap branch depends on the host: each
test asserts what the probe said, so it holds on a host with and without bubblewrap.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from aksha.adapters.egress.network import SocketResolver
from aksha.adapters.sandbox.bwrap import BwrapSandbox
from aksha.adapters.sandbox.e2b import E2BSandbox
from aksha.adapters.sandbox.workspace import SessionFolders
from aksha.app import RuntimeConfig, execution_tier
from aksha.interfaces.cli.main import main
from aksha.runtime.sandbox.refuse import RefuseSandbox
from aksha.runtime.sandbox.types import SessionRef
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


# --- decision 37: one stateful sandbox per session ------------------------------------------


def test_none_is_the_same_as_unset() -> None:
    tier = _tier(AKSHA_SANDBOX="none")
    assert tier.box is None and tier.gate_availability is None


def test_session_workspaces_live_under_the_data_directory(tmp_path: Path) -> None:
    repo = tmp_path / "SYN-project"
    repo.mkdir()
    tier = _tier(AKSHA_SANDBOX="bwrap", AKSHA_HOME=str(tmp_path / "home"),
                 AKSHA_WORKSPACE=str(repo))  # fmt: skip
    if not HOST_HAS_BWRAP:
        assert isinstance(tier.box, RefuseSandbox)
        return
    assert isinstance(tier.box, BwrapSandbox) and tier.box.folders is not None
    assert tier.box.folders.root == tmp_path / "home" / "sandbox" / "bwrap"
    assert tier.box.folders.origin == repo.resolve()
    config = RuntimeConfig.from_env({"AKSHA_SANDBOX_PATH": str(tmp_path / "boxes")})
    assert config.sandboxes.root == tmp_path / "boxes"


def test_e2b_is_chosen_only_by_name_and_probes_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    off = _tier(AKSHA_SANDBOX="e2b")
    assert isinstance(off.box, RefuseSandbox)
    assert off.gate_availability is not None and "E2B_API_KEY" in off.gate_availability.reason
    monkeypatch.setenv("E2B_API_KEY", "SYN-e2b-key-1111")
    on = _tier(AKSHA_SANDBOX="e2b:vm", AKSHA_E2B_ON_END="kill")
    assert isinstance(on.box, E2BSandbox) and on.gate_availability is not None
    assert on.gate_availability.available and on.gate_availability.strength == "vm"
    assert "SYN-e2b-key-1111" not in json.dumps(on.record())
    auto = _tier(AKSHA_SANDBOX="auto")
    assert [p.backend for p in auto.probes] == ["bwrap"]  # auto never picks a cloud


@pytest.mark.parametrize(("name", "value"), [("AKSHA_E2B_ON_END", "sleep"),
                                             ("AKSHA_E2B_TIMEOUT_S", "soon")])  # fmt: skip
def test_bad_e2b_settings_are_refused(name: str, value: str) -> None:
    with pytest.raises(StoreError) as refused:
        RuntimeConfig.from_env({name: value})
    assert refused.value.code == "sandbox_invalid"


def test_aksha_sandbox_lists_and_cleans_a_session(tmp_path: Path) -> None:
    env = {"AKSHA_HOME": str(tmp_path / "home")}
    folders = SessionFolders(tmp_path / "home" / "sandbox" / "bwrap")
    work, _, _ = folders.ensure(SessionRef("syn-user", "S-0abc"))
    folders.ensure(SessionRef("someone-else", "S-0def"))

    def cli(*argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = main(["--user", "syn-user", "sandbox", *argv], out=out, env=env)
        return code, out.getvalue()

    assert cli("list") == (0, "S-0abc  bwrap\n")
    code, said = cli("clean", "S-0abc")
    assert code == 0 and f"bwrap: removed {work}" in said and not work.exists()
    assert cli("clean", "S-0abc") == (0, "S-0abc has no session workspace\n")
    assert folders.sessions() == (SessionRef("someone-else", "S-0def"),)
