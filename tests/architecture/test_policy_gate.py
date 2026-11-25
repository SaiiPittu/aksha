"""ADR-P11's static tests: no bypass path, no model text in the gate input, a pure gate.

Each guard is also run on a planted violation, so it is seen to fail before it is trusted.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import pkgutil
import shutil
from pathlib import Path

import pytest

import aksha.runtime
from aksha.runtime.artifacts.types import SaveRequest
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.policy.gate import check
from aksha.runtime.policy.types import Allow, GateInput, Refuse
from aksha.runtime.projections.view import ArtifactsView, LogView
from aksha.runtime.storage.rows import ArtifactRow, StoredArtifact
from aksha.runtime.tools.types import Err, Ok, ToolOutcome, ToolResultView
from tests.architecture.rules import (
    Violation,
    bypass_paths,
    impure_gate,
    model_text_in,
    policy_writers,
)

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"  # the source root: the folder that holds the aksha package

MODEL_TEXT: frozenset[type] = frozenset(
    {Ok, Err, ToolOutcome, ToolResultView, SaveRequest, Event, NewEvent, LogView,
     ArtifactsView, ArtifactRow, StoredArtifact}
)  # fmt: skip
"""Types that carry a tool result, an artifact's content or stored text."""


def _namespace() -> dict[str, object]:
    names: dict[str, object] = {}
    for info in pkgutil.walk_packages(aksha.runtime.__path__, "aksha.runtime."):
        names.update(vars(importlib.import_module(info.name)))
    return names


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    shutil.copytree(SRC / "aksha", tmp_path / "aksha", ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


# --- test_gate_inputs_hold_no_model_text ------------------------------------------------


def test_gate_inputs_hold_no_model_text() -> None:
    assert model_text_in(GateInput, MODEL_TEXT, _namespace()) == []
    names = {f.name for f in dataclasses.fields(GateInput)}
    assert not names & {"result", "results", "content", "text", "message", "messages", "memory"}


def test_model_text_guard_catches_a_planted_field() -> None:
    @dataclasses.dataclass(frozen=True)
    class Planted:
        ok: int
        last: tuple[Ok | None, ...]

    @dataclasses.dataclass(frozen=True)
    class Outer:
        inner: Planted

    namespace = {**_namespace(), "Planted": Planted}
    assert model_text_in(Outer, MODEL_TEXT, namespace) == ["Outer.inner.last"]


def test_check_takes_only_the_gate_input_and_the_registry() -> None:
    assert list(inspect.signature(check).parameters) == ["g", "registry"]


# --- test_no_bypass_path ----------------------------------------------------------------


def test_no_bypass_path() -> None:
    assert [str(v) for v in bypass_paths(SRC)] == []


PLANT_BYPASS = '''"""Planted: a body run with no gate call at all."""
from aksha.runtime.tools.bodies import call_body


def run_it(decl: object, ctx: object, args: object) -> object:
    return call_body(decl, ctx, args)
'''

PLANT_TRUTHY = '''"""Planted: check is called, but a truthy Refuse runs the body."""
from aksha.runtime.policy.gate import check
from aksha.runtime.tools.bodies import call_body


def run_it(g: object, registry: object, decl: object, ctx: object) -> object:
    if check(g, registry):
        return call_body(decl, ctx, {})
    return None
'''

PLANT_IGNORED = '''"""Planted: the answer is narrowed, but the body runs on the other branch."""
from aksha.runtime.policy.gate import check
from aksha.runtime.policy.types import Allow
from aksha.runtime.tools.bodies import call_body


def run_it(g: object, registry: object, decl: object, ctx: object) -> object:
    decision = check(g, registry)
    if isinstance(decision, Allow):
        return None
    else:
        return call_body(decl, ctx, {})
'''

PLANT_CHECKED = '''"""Planted: the three forms the guard accepts."""
from aksha.runtime.policy.gate import check
from aksha.runtime.policy.types import Allow
from aksha.runtime.tools.bodies import call_body


def by_if(g: object, registry: object, decl: object, ctx: object) -> object:
    decision = check(g, registry)
    if isinstance(decision, Allow):
        return call_body(decl, ctx, {})
    return decision


def by_match(g: object, registry: object, decl: object, ctx: object) -> object:
    decision = check(g, registry)
    match decision:
        case Allow():
            return call_body(decl, ctx, {})
        case _:
            return decision


def by_early_exit(g: object, registry: object, decl: object, ctx: object) -> object:
    decision = check(g, registry)
    if not isinstance(decision, Allow):
        return decision
    return call_body(decl, ctx, {})
'''


def test_bypass_guard_catches_a_body_called_without_an_allow(copy: Path) -> None:
    (copy / "aksha/runtime/planted_bypass.py").write_text(PLANT_BYPASS)
    (copy / "aksha/runtime/planted_truthy.py").write_text(PLANT_TRUTHY)
    (copy / "aksha/runtime/planted_ignored.py").write_text(PLANT_IGNORED)
    (copy / "aksha/runtime/planted_checked.py").write_text(PLANT_CHECKED)
    rule = "every body runs after an Allow"
    assert bypass_paths(copy) == [
        Violation("aksha.runtime.planted_bypass", "run_it", rule),
        Violation("aksha.runtime.planted_ignored", "run_it", rule),
        Violation("aksha.runtime.planted_truthy", "run_it", rule),
    ]


def test_a_gate_answer_has_no_truth_value() -> None:
    for answer in (Allow("dispatch"), Refuse("narrowed", 4, "no", counted=True)):
        with pytest.raises(TypeError, match="isinstance"):
            bool(answer)


# --- the gate is pure (D1) --------------------------------------------------------------


def test_gate_modules_are_pure() -> None:
    assert [str(v) for v in impure_gate(SRC)] == []


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("import os\nMODE = os.environ.get('AKSHA_BYPASS')\n", "os"),
        ("import time\n", "time"),
        ("def f(store: object) -> object:\n    return store.read('o')\n", "read"),
    ],
)
def test_purity_guard_catches_a_plant(copy: Path, source: str, found: str) -> None:
    gate = copy / "aksha/runtime/policy/gate.py"
    gate.write_text(gate.read_text() + "\n" + source)
    assert found in [v.target for v in impure_gate(copy)]


# --- the gate never appends; the step executor records the decision (D9) ---------------


def test_policy_package_never_appends() -> None:
    assert [str(v) for v in policy_writers(SRC)] == []


def test_append_guard_catches_a_planted_store_import(copy: Path) -> None:
    planted = copy / "aksha/runtime/policy/events.py"
    planted.write_text(
        planted.read_text() + "\nfrom aksha.runtime.storage.ports import EventStore\n"
    )
    assert [(v.module, v.target) for v in policy_writers(copy)] == [
        ("aksha.runtime.policy.events", "aksha.runtime.storage.ports")
    ]
