"""``make demo`` end to end: one command, offline, and the story's claims hold (CI runs it).

The demo runs as the user runs it, ``python -m aksha.interfaces.demo``, in a child process
with a real ``SIGKILL`` inside it; its ``--json`` report is checked against the variant
world's answer key and against what the mock mailbox actually received. It fails when a
change reopens work outside C1's chain, when research is touched, when recovery sends
again, or when the hash chain does not verify.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aksha.interfaces.demo.world import EDITS, VARIANT, WORLDS, apply_change, copy_base

ROOT = Path(__file__).resolve().parents[2]
C1_CHAIN = {
    f"patent.{kind}/C1@v2"
    for kind in ("extraction", "prior_art", "comparison", "recommendation", "disclosure",
                 "worksheet", "packet", "handoff")
} | {"patent.table@v2", "patent.selection@v2"}  # fmt: skip


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], str]:
    d = tmp_path_factory.mktemp("demo")
    out = subprocess.run(
        [sys.executable, "-m", "aksha.interfaces.demo", "--work", str(d / "work"),
         "--json", str(d / "report.json")],
        capture_output=True, text=True, cwd=ROOT, timeout=300, check=False,
    )  # fmt: skip
    assert out.returncode == 0, out.stderr[-4000:]
    report: dict[str, Any] = json.loads((d / "report.json").read_text(encoding="utf-8"))
    return report, out.stdout


def test_the_change_reopens_only_c1s_chain_and_keeps_the_rest(
    demo: tuple[dict[str, Any], str],
) -> None:
    report, _ = demo
    assert set(report["rebuilt"]) == C1_CHAIN
    first, kept = report["first"], report["kept"]
    assert report["kept"]["research"] == sorted(first["research"])  # every research output
    others = {s for s in first["patent"] if s.split("/")[-1] in ("C2", "C3", "C4")}
    assert others and others <= set(kept["patent"])
    assert {r["agent"] for r in report["runs"].values() if r["cause"] == "rebuild"} == {"patent"}
    change = report["change"]
    # Every recorded query re-runs: four candidate searches, the candidate and template
    # listings, and the research agent's two listings and two searches. Only C1's search
    # finds the new publication; the new file is no seller page or review export, so the
    # research listings stay as they were.
    assert (change["queries_rerun"], change["queries_changed"]) == (10, 1)
    key = json.loads((WORLDS / VARIANT / "key.json").read_text("utf-8"))
    action = key["expected_after_change"]["C1_next_action"]
    assert report["next_action"]["before"] == action["before"]
    assert report["next_action"]["after"] == action["after"]


def test_a_fresh_approval_is_asked_for_the_new_packet_and_scripted(
    demo: tuple[dict[str, Any], str],
) -> None:
    report, stdout = demo
    first, fresh = report["approvals"]
    assert first["targets"] == ["patent.packet/C1@v1"]
    assert fresh["targets"] == ["patent.packet/C1@v2"]
    assert first["approval"] != fresh["approval"]
    assert {a["by"] for a in report["approvals"]} == {"demo-script"}
    assert "scripted" in stdout


def test_the_sigkill_mid_send_recovers_to_exactly_one_send_per_packet(
    demo: tuple[dict[str, Any], str],
) -> None:
    report, _ = demo
    kill = report["kill"]
    assert kill["killed"] and kill["returncode"] == -9
    assert kill["rows_at_kill"] == 2  # the mailbox stored v2 before the process died
    assert kill["outbox_open_at_restart"] == 1  # and the runtime had no receipt for it
    assert kill["recovery_lookups"] >= 1 and kill["recovery_applies"] == 0
    mailbox = report["mailbox"]
    assert mailbox["packets"] == ["patent.packet/C1@v1", "patent.packet/C1@v2"]
    assert mailbox["deliveries"] == mailbox["keys"] == 2
    assert all(r["outcome"] == "complete" for r in report["runs"].values())


def test_the_chain_verifies_and_the_demo_is_quick(demo: tuple[dict[str, Any], str]) -> None:
    report, stdout = demo
    assert report["chain"]["ok"] and report["chain"]["events"] > 0
    assert report["seconds"] < 120
    assert len(stdout.splitlines()) < 60  # readable in under a minute


def test_the_change_set_is_the_variant_keys_change_and_nothing_else(tmp_path: Path) -> None:
    sources = copy_base(tmp_path)
    before = {p.relative_to(sources): p.read_bytes() for p in sources.rglob("*") if p.is_file()}
    change = apply_change(sources)
    after = {p.relative_to(sources): p.read_bytes() for p in sources.rglob("*") if p.is_file()}
    variant = WORLDS / VARIANT / "sources"
    assert {str(p) for p in after.keys() - before.keys()} == set(change.added)
    moved = {str(p) for p in before if before[p] != after[p]}
    assert moved == {rel for rel, _, _ in EDITS}
    for rel, _, new in EDITS:  # each edited line is the variant's line, byte for byte
        assert new.encode() in (variant / rel).read_bytes()
    for rel in change.added:
        assert (sources / rel).read_bytes() == (variant / rel).read_bytes()


def test_an_existing_folder_the_demo_did_not_write_is_refused(tmp_path: Path) -> None:
    keep = tmp_path / "notes.txt"
    keep.write_text("not the demo's", encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "-m", "aksha.interfaces.demo", "--work", str(tmp_path)],
        capture_output=True, text=True, cwd=ROOT, timeout=60, check=False,
    )  # fmt: skip
    assert out.returncode == 2 and "not written by the demo" in out.stderr
    assert keep.read_text(encoding="utf-8") == "not the demo's"


def test_the_readmes_demo_excerpt_matches_a_run(demo: tuple[dict[str, Any], str]) -> None:
    """The numbers the README quotes from the demo are the ones a run reports."""
    report, stdout = demo
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    research, patent = len(report["kept"]["research"]), len(report["kept"]["patent"])
    deliveries = report["mailbox"]["deliveries"]
    quoted = (
        f"rebuilt {len(report['rebuilt'])}: ",
        f"kept {patent} patent outputs: ",
        f"kept all {research} research outputs: nothing they read moved",
        f"mailbox: {deliveries} deliveries for {report['mailbox']['keys']} idempotency keys",
        "recovery asked the mailbox by key 1 time, sent 0 times",
    )
    for line in quoted:
        assert line in readme, line
        assert line in stdout, line
