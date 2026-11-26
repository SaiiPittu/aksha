"""The patent agent's approved send from the command line, and no wedged owner after a refusal.

1. With no system of record for the effect, ``aksha approve`` is refused
   ``system_unavailable`` with a stored receipt: nothing is approved, the run still
   waits for the same approval, and the owner's other commands (a new ``aksha run``) work.
2. With ``AKSHA_SYSTEMS=counsel_mail=mailbox:<dir>`` (the opt-in mock mailbox), the same
   approval delivers the packet exactly once, and the output says the mailbox is a mock.
3. A run already in flight whose effect has no system (approved by a process that had the
   mailbox, resumed by one that has not) does not stop the owner's other commands; once
   the mailbox is configured again, ``aksha resume`` sends the packet once.
4. ``aksha answer`` and ``aksha status`` show the question text a person is asked.
"""

from __future__ import annotations

import io
import json
import re
import shutil
from pathlib import Path

import pytest

from aksha.app import DEFAULT_AGENTS, DEFAULT_EXTENSIONS, RuntimeConfig, open_session
from aksha.interfaces import wire
from aksha.interfaces.cli.main import main
from aksha.runtime.session import named as n
from tests.support.session import env

REPO = Path(__file__).resolve().parents[2]
WORLDS = REPO / "fixtures" / "worlds" / "stillroom" / "sources"
REQUEST = "Assess the four invention candidates and prepare the chosen one for counsel."
GAP = "0.5 m from the vent edge, so its air flow stays outside the footprint."
RESEARCH = "Who sells camera-free occupancy sensing for offices, and at what price?"


def defaults(**extra: str) -> dict[str, str]:
    out = {
        "AKSHA_AGENTS": str(DEFAULT_AGENTS),
        "AKSHA_EXTENSIONS": str(DEFAULT_EXTENSIONS),
        "AKSHA_SOURCES": f"patent_sources={WORLDS}:corpus={WORLDS}",
    }
    out.update(extra)
    return out


def aksha(tmp: Path, *argv: str, **extra: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(["--user", "o1", *argv], out=out, env=env(tmp, **defaults(**extra)))
    return code, out.getvalue()


@pytest.fixture(scope="module")
def at_approval(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, str, str]:
    """One patent run driven to its send approval; each test copies the folder."""
    tmp = tmp_path_factory.mktemp("patent")
    code, out = aksha(tmp, "run", REQUEST, "--agent", "patent", "--session", "S-1")
    assert code == wire.EXIT_WAITING_INPUT, out
    assert "C2: the note leaves Minimum distance from a vent undecided" in out, out
    gap = re.search(r"aksha answer (I-\d+)", out)
    assert gap is not None
    code, out = aksha(tmp, "answer", gap.group(1), GAP)
    assert code == wire.EXIT_WAITING_INPUT, out
    assert "Which candidate should be prepared for counsel review?" in out, out
    assert "options: C1, C2, C3, C4" in out, out
    choice = re.search(r"aksha answer (I-\d+)", out)
    assert choice is not None
    code, out = aksha(tmp, "answer", choice.group(1), "C1")
    assert code == wire.EXIT_WAITING_APPROVAL, out
    found = re.search(r"aksha approve (A-\d+) --signature (v1:[0-9a-f]{64})", out)
    assert found is not None
    assert "send_packet" in out and "patent.packet/C1@v1" in out, out
    return tmp, found.group(1), found.group(2), out


def _copy(src: Path, tmp: Path) -> Path:
    shutil.copytree(src / "var", tmp / "var")
    return tmp


def _deliveries(folder: Path) -> int:
    ledger = folder / "mailbox.json"
    return len(json.loads(ledger.read_text(encoding="utf-8"))) if ledger.exists() else 0


def test_status_shows_what_the_approval_would_do(
    at_approval: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    tmp = _copy(at_approval[0], tmp_path)
    code, out = aksha(tmp, "status")
    assert code == wire.EXIT_OK
    assert "send_packet" in out and "recipient: counsel-primary" in out, out


def test_a_refused_approval_leaves_the_run_waiting_and_other_commands_work(
    at_approval: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    src, approval, signature, _ = at_approval
    tmp = _copy(src, tmp_path)
    code, out = aksha(tmp, "approve", approval, "--signature", signature)
    assert code == wire.EXIT_REFUSED, out
    assert "system_unavailable" in out and "AKSHA_SYSTEMS=counsel_mail=mailbox:" in out, out
    code, out = aksha(tmp, "status", "R-0001")
    assert code == wire.EXIT_WAITING_APPROVAL, out
    assert f"waiting for approval {approval}" in out
    code, out = aksha(tmp, "run", RESEARCH, "--agent", "research", "--session", "S-2")
    assert code == wire.EXIT_WAITING_INPUT, out
    assert "R-0002 research waiting" in out
    box = tmp / "mailbox"
    code, out = aksha(tmp, "approve", approval, "--signature", signature,
                      AKSHA_SYSTEMS=f"counsel_mail=mailbox:{box}")  # fmt: skip
    assert code == wire.EXIT_OK, out
    assert "R-0001 patent ended complete" in out
    assert _deliveries(box) == 1


def test_an_approved_send_goes_to_the_mock_mailbox_exactly_once(
    at_approval: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    src, approval, signature, _ = at_approval
    tmp = _copy(src, tmp_path)
    box = tmp / "mailbox"
    setting = {"AKSHA_SYSTEMS": f"counsel_mail=mailbox:{box}"}
    code, out = aksha(tmp, "approve", approval, "--signature", signature[:16], **setting)
    assert code == wire.EXIT_OK, out
    assert "mock" in out and str(box) in out, out
    assert "R-0001 patent ended complete" in out
    assert _deliveries(box) == 1
    code, out = aksha(tmp, "approve", approval, "--signature", signature, **setting)
    assert code == wire.EXIT_REFUSED, out
    code, out = aksha(tmp, "resume", **setting)
    assert _deliveries(box) == 1


def test_an_in_flight_send_with_no_system_does_not_wedge_the_owner(
    at_approval: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    src, approval, signature, _ = at_approval
    tmp = _copy(src, tmp_path)
    box = tmp / "mailbox"
    setting = defaults(AKSHA_SYSTEMS=f"counsel_mail=mailbox:{box}")
    with_box = RuntimeConfig.from_env({**env(tmp), **setting})
    session = open_session(with_box)
    try:  # approved by a process with the mailbox, which dies before it drives the run
        grant = n.Approve(owner="o1", approval_id=approval, shown_signature=signature,
                          command_id="c-1")  # fmt: skip
        receipt = session.dispatch(grant)
        assert receipt.accepted, receipt
    finally:
        session.store.close()
    code, out = aksha(tmp, "run", RESEARCH, "--agent", "research", "--session", "S-2")
    assert code == wire.EXIT_WAITING_INPUT, out
    assert "R-0002 research waiting" in out
    assert "R-0001" in out and "system_unavailable" in out, out
    code, out = aksha(tmp, "status")
    assert code == wire.EXIT_OK and "R-0001 patent running" in out, out
    assert _deliveries(box) == 0
    code, out = aksha(tmp, "resume", "R-0001", AKSHA_SYSTEMS=f"counsel_mail=mailbox:{box}")
    assert code == wire.EXIT_OK and "R-0001 patent ended complete" in out, out
    assert _deliveries(box) == 1


def test_an_unknown_or_malformed_systems_setting_is_refused(tmp_path: Path) -> None:
    code, out = aksha(tmp_path, "run", RESEARCH, "--agent", "research",
                      AKSHA_SYSTEMS=f"nowhere=mailbox:{tmp_path}")  # fmt: skip
    assert code == wire.EXIT_ENVIRONMENT and "system_unknown" in out, out
    code, out = aksha(tmp_path, "run", RESEARCH, "--agent", "research",
                      AKSHA_SYSTEMS=f"counsel_mail=smtp:{tmp_path}")  # fmt: skip
    assert code == wire.EXIT_ENVIRONMENT and "system_invalid" in out, out
