"""The eval case files under ``evals/cases/`` are well-formed data (eval design, section 2).

Only the shape is checked here; the evals runner runs the cases. The resume-against-fresh
case (decision 36) must name settings and fresh reasons the runtime records.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.context.reentry import FRESH_REASONS, REENTRY_SETTINGS

CASES = Path(__file__).resolve().parents[2] / "evals" / "cases"
REQUIRED = ("id", "kind", "origin", "source", "category", "agents", "worlds")


def cases() -> list[Path]:
    return sorted(CASES.glob("*.json"))


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def test_there_is_a_case_directory() -> None:
    assert cases(), CASES


@pytest.mark.parametrize("path", cases(), ids=lambda p: p.stem)
def test_case_fields_and_pairing_rules(path: Path) -> None:
    case = load(path)
    for field in REQUIRED:
        assert case.get(field), (path.name, field)
    assert case["id"] == path.stem
    assert case["kind"] in {"bad", "guard", "control"}
    assert case["origin"] in {"hand_written", "real_failure", "planted_trap"}
    if case["origin"] == "real_failure":
        assert case["kind"] == "bad"
    if case["kind"] == "control":
        assert case["origin"] == "hand_written"
    if case["origin"] == "planted_trap":
        assert any(e.startswith("traps.") for e in case.get("expect", ()))
    assert case.get("relation", "none") in {"none", "invariance", "directional"}


def test_the_resume_case_names_what_the_runtime_records() -> None:
    case = load(CASES / "change-scope-resume-vs-fresh.json")
    arms = {a["name"]: a for a in case["arms"]}
    assert {"fresh", "resume"} <= set(arms)
    for arm in arms.values():
        assert arm["run_started"]["reentry"] in REENTRY_SETTINGS
        assert set(arm["expect_reentry"]["modes"]) <= {"fresh", "resume"}
        assert set(arm["expect_reentry"]["reasons"]) <= FRESH_REASONS
    assert arms["fresh"]["expect_reentry"] == {"modes": ["fresh"], "reasons": ["resume_disabled"]}
    assert "resume_disabled" not in arms["resume"]["expect_reentry"]["reasons"]
    for pair in case["compare"]:
        assert set(pair) <= set(arms)
    measured = {m["id"] for m in case["metrics"]}
    assert {"correctness", "model_turns", "tool_calls", "input_tokens", "output_tokens"} <= measured
