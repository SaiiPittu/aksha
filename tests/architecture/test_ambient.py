"""ADR-P02 D3: the runtime core reads no ambient clock or randomness; plants are caught."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.architecture.rules import AMBIENT_ALLOWED, ambient_clock_or_randomness

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    shutil.copytree(SRC / "aksha", tmp_path / "aksha", ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def test_runtime_uses_no_ambient_clock_or_randomness() -> None:
    assert [str(v) for v in ambient_clock_or_randomness(SRC)] == []


def test_the_one_exemption_is_still_needed() -> None:
    text = (SRC / "aksha/runtime/storage/chain.py").read_text(encoding="utf-8")
    assert ("aksha.runtime.storage.chain", "secrets") in AMBIENT_ALLOWED
    assert "import secrets" in text


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("import random\n", "random"),
        ("from uuid import uuid4\n", "uuid"),
        ("import secrets\n", "secrets"),
        ("import time\nx = time.time()\n", "time.time"),
        ("import time\ntime.sleep(1)\n", "time.sleep"),
        ("from time import sleep\n", "time.sleep"),
        ("from time import monotonic as m\n", "time.monotonic"),
        ("from datetime import datetime\nx = datetime.now()\n", "datetime.now"),
        ("import datetime\nx = datetime.datetime.now()\n", "datetime.now"),
        ("from datetime import date\nx = date.today()\n", "date.today"),
    ],
)
def test_ambient_guard_catches_a_plant(copy: Path, source: str, found: str) -> None:
    (copy / "aksha/runtime/execution/planted.py").write_text(f'"""Planted."""\n{source}')
    assert found in [v.target for v in ambient_clock_or_randomness(copy)]


def test_clock_values_through_the_port_pass(copy: Path) -> None:
    source = '"""Planted."""\ndef f(clock):\n    clock.sleep(1)\n    return clock.now()\n'
    (copy / "aksha/runtime/execution/planted.py").write_text(source)
    assert ambient_clock_or_randomness(copy) == []
