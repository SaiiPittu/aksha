"""ADR-P14 D1: no runtime object holds a counter for a cap; a planted one is caught."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.architecture.rules import counter_fields

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    shutil.copytree(SRC / "aksha", tmp_path / "aksha", ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def test_no_counter_fields() -> None:
    assert [str(v) for v in counter_fields(SRC)] == []


@pytest.mark.parametrize(
    ("body", "found"),
    [
        ("self.used += 1", "self.used"),
        ("self.calls[name] -= 1", "self.calls"),
        ("self.turns = self.turns + 1", "self.turns"),
        ("self.turns = 1 + self.turns", "self.turns"),
    ],
)
def test_counter_guard_catches_a_plant(copy: Path, body: str, found: str) -> None:
    source = f'"""Planted."""\nclass Loop:\n    def step(self, name):\n        {body}\n'
    (copy / "aksha/runtime/budgets/planted.py").write_text(source)
    assert found in [v.target for v in counter_fields(copy)]


def test_a_local_count_is_not_a_field(copy: Path) -> None:
    source = (
        '"""Planted."""\ndef f(xs):\n    n = 0\n    for _ in xs:\n        n += 1\n    return n\n'
    )
    (copy / "aksha/runtime/budgets/planted.py").write_text(source)
    assert counter_fields(copy) == []
