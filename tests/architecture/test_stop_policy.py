"""ADR-P16 D1's static test: every deciding stop-policy module is pure.

``test_policies_are_pure``: no module under ``runtime/stop_policy/`` but ``commit.py``
imports a clock, randomness, I/O, an adapter or the event store, or calls the clock. The
guard is run on planted violations first, so it is seen to fail before it is trusted.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.architecture.rules import impure_stop_policies

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    shutil.copytree(SRC / "aksha", tmp_path / "aksha", ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def test_policies_are_pure() -> None:
    assert [str(v) for v in impure_stop_policies(SRC)] == []


@pytest.mark.parametrize(
    ("module", "source", "found"),
    [
        ("core.py", "import time\n", "time"),
        ("evaluation.py", "from aksha.runtime.storage.ports import EventStore\n",
         "aksha.runtime.storage.ports"),
        ("builtins.py", "def f(clock: object) -> object:\n    return clock.now()\n", "now"),
        ("hold.py", "from aksha.adapters.clock.system import SystemClock\n",
         "aksha.adapters.clock.system"),
    ],
)  # fmt: skip
def test_purity_guard_catches_a_plant(copy: Path, module: str, source: str, found: str) -> None:
    target = copy / "aksha/runtime/stop_policy" / module
    target.write_text(target.read_text() + "\n" + source)
    assert found in [v.target for v in impure_stop_policies(copy)]


def test_the_writer_is_the_one_exemption(copy: Path) -> None:
    writer = copy / "aksha/runtime/stop_policy/commit.py"
    assert writer.exists()
    assert impure_stop_policies(copy) == []
