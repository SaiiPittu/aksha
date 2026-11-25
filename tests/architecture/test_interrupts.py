"""ADR-P17's and ADR-P11 D12's static tests, each also run on a planted violation.

1. ``test_action_body_has_no_agent_fields``: the action body holds only the generic set.
2. ``test_approval_facts_are_pure``: the suite's ``ApprovalRule.facts`` runs on a bare
   ``LogView`` (no store, clock or model to reach) and returns the same facts twice.
3. ``test_no_bypass_path_headless``: ``headless`` is read nowhere but at ``resolve_ask``.
"""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest

from aksha.runtime.interrupts.approvals import approval_signature
from aksha.runtime.interrupts.types import ACTION_BODY_FIELDS, ActionBody
from aksha.runtime.projections.view import ArtifactsView, LogView
from tests.architecture.rules import headless_reads
from tests.support.approvals import SEND, body

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    shutil.copytree(SRC / "aksha", tmp_path / "aksha", ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def test_action_body_has_no_agent_fields() -> None:
    assert {f.name for f in dataclasses.fields(ActionBody)} == ACTION_BODY_FIELDS
    assert set(body().as_json()) == ACTION_BODY_FIELDS
    assert approval_signature(body()).startswith("v1:")


def test_approval_facts_are_pure() -> None:
    """The rule reads only the view it is given: no store, clock or model is in reach."""
    rule = SEND.spec.approval
    assert rule is not None
    empty = LogView("o", 0, (), ArtifactsView((), 0))
    facts = rule.facts(empty, {"packet": "p.packet/C1@v1", "to": "R1"})
    assert facts == {"recipient": "R1", "recipient_version": "none"}
    assert rule.facts(empty, {"packet": "p.packet/C1@v1", "to": "R1"}) == facts  # repeatable


def test_no_bypass_path_headless() -> None:
    assert [str(v) for v in headless_reads(SRC)] == []


def test_headless_guard_catches_a_planted_read(copy: Path) -> None:
    planted = copy / "aksha/runtime/policy/gate.py"
    planted.write_text(planted.read_text() + "\n\ndef _skip(g):\n    return g.headless\n")
    assert [v.module for v in headless_reads(copy)] == ["aksha.runtime.policy.gate"]
