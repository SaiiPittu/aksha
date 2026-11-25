"""Runtime reducers are pure (ADR-P01 `test_runtime_reducers_are_pure`); plants are caught."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.architecture.rules import Violation, impure_reducers

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"  # the source root: the folder that holds the aksha package

PURE = '''"""Planted."""
class Counter:
    name = "counter"
    reads = frozenset({"probe.noted"})
    def initial(self, args):
        return 0
    def apply(self, state, event, artifacts):
        return state + 1
'''


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    shutil.copytree(SRC / "aksha", tmp_path / "aksha", ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def test_runtime_reducers_are_pure() -> None:
    assert [str(v) for v in impure_reducers(SRC)] == []


def test_a_pure_plant_passes(copy: Path) -> None:
    (copy / "aksha/runtime/projections/planted.py").write_text(PURE)
    assert impure_reducers(copy) == []


@pytest.mark.parametrize(
    ("source", "found"),
    [
        (PURE.replace("return state + 1", "return state + clock.now().second"), "Counter.now"),
        (
            PURE.replace("return state + 1", "return store.last_seq(event.owner)"),
            "Counter.last_seq",
        ),
        (PURE.replace("return 0", "return len(open('x').read())"), "Counter.open"),
        (
            PURE.replace("return state + 1", "state.append(1)\n        return state"),
            "Counter.append",
        ),
    ],
)
def test_purity_guard_catches_an_impure_call(copy: Path, source: str, found: str) -> None:
    (copy / "aksha/runtime/projections/planted.py").write_text(source)
    assert found in [v.target for v in impure_reducers(copy)]


def test_purity_guard_catches_an_impure_import(copy: Path) -> None:
    (copy / "aksha/runtime/projections/planted.py").write_text("import random\n" + PURE)
    assert impure_reducers(copy) == [
        Violation(
            "aksha.runtime.projections.planted",
            "random",
            "a reducer's module imports no clock or I/O",
        )
    ]
