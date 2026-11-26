"""ADR-P06 D3: the agent loop writes no event of its own; a planted write is caught."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.architecture.rules import loop_writers

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    shutil.copytree(SRC / "aksha", tmp_path / "aksha", ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def test_loop_writes_only_through_the_executor() -> None:
    assert [str(v) for v in loop_writers(SRC)] == []


@pytest.mark.parametrize(
    ("module", "source", "found"),
    [
        ("turn.py", "def f(store, events):\n    store.append('o', 'U2', events)\n", "append"),
        ("calls.py", "def f(conn):\n    conn.commit()\n", "commit"),
        (
            "turn.py",
            "def f(c, s, o, d):\n    c.commit_stop_decision(s, o, d)\n",
            "commit_stop_decision",
        ),
        ("drive.py", "def f(s, o, d):\n    commit_task_facts(s, o, d)\n", "commit_task_facts"),
    ],
)
def test_write_guard_catches_a_plant(copy: Path, module: str, source: str, found: str) -> None:
    planted = copy / "aksha/runtime/loop" / module
    planted.write_text(planted.read_text() + "\n\n" + source)
    assert found in [v.target for v in loop_writers(copy)]


def test_a_list_append_is_not_a_write(copy: Path) -> None:
    planted = copy / "aksha/runtime/loop/calls.py"
    planted.write_text(planted.read_text() + "\n\ndef f(out):\n    out.append(1)\n")
    assert loop_writers(copy) == []
