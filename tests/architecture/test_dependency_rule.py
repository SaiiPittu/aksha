"""The dependency rule and layout rules hold for the real package, and each fails on a plant."""

from __future__ import annotations

import importlib
import pkgutil
import shutil
from pathlib import Path

import pytest

import aksha
from tests.architecture.rules import (
    Violation,
    check_dependency_rule,
    imports_of,
    module_name,
    modules_missing_from_doc,
    protocols_outside_ports,
    tree_paths,
)

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"  # the source root: the folder that holds the aksha package
DOC = REPO / "docs" / "architecture" / "codebase-structure.md"


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    """A throwaway copy of the package to plant violations in."""
    shutil.copytree(SRC / "aksha", tmp_path / "aksha", ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def _plant(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    for parent in path.relative_to(root).parents:
        init = root / parent / "__init__.py"
        if str(parent) != "." and not init.exists():
            init.write_text('"""Planted package."""\n')
    path.write_text(source)


# --- The real package -------------------------------------------------------------------


def test_package_keeps_the_dependency_rule() -> None:
    assert [str(v) for v in check_dependency_rule(SRC)] == []


def test_protocols_live_only_in_ports_modules() -> None:
    assert [str(v) for v in protocols_outside_ports(SRC)] == []


def test_every_module_is_listed_in_the_structure_doc() -> None:
    assert modules_missing_from_doc(SRC, DOC) == []


def test_every_module_imports_and_has_a_docstring() -> None:
    names = [m.name for m in pkgutil.walk_packages(aksha.__path__, "aksha.")]
    assert "aksha.runtime.storage.ports" in names
    for name in names:
        module = importlib.import_module(name)
        assert module.__doc__ and module.__doc__.strip(), name


# --- Each guard fails on a planted violation ----------------------------------------------


@pytest.mark.parametrize(
    ("rel", "source", "rule"),
    [
        (
            "aksha/runtime/tools/leak.py",
            "from aksha.adapters.storage import sqlite\n",
            "runtime imports no outer layer",
        ),
        ("aksha/data/leak.py", "from aksha import app\n", "data imports nothing of aksha"),
        (
            "aksha/runtime/tools/leak.py",
            "from ... import interfaces\n",
            "runtime imports no outer layer",
        ),
        (
            "aksha/runtime/tools/leak.py",
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import aksha.agents\n",
            "runtime imports no outer layer",
        ),
        (
            "aksha/runtime/tools/leak.py",
            "import psycopg\n",
            "runtime imports the standard library and jsonschema only",
        ),
        (
            "aksha/runtime/session/leak.py",
            "import fastapi\n",
            "runtime imports the standard library and jsonschema only",
        ),
        (
            "aksha/runtime/session/leak.py",
            "from opentelemetry import trace\n",
            "runtime imports the standard library and jsonschema only",
        ),
        (
            "aksha/interfaces/http/leak.py",
            "from aksha.extensions.otel import trace\n",
            "interfaces reach adapters, agents and extensions through aksha.app only",
        ),
        (
            "aksha/adapters/storage/leak.py",
            "from aksha.adapters.clock import fake\n",
            "adapters import runtime ports and their own family only",
        ),
        (
            "aksha/adapters/storage/leak.py",
            "import aksha.interfaces\n",
            "adapters import runtime ports and their own family only",
        ),
        (
            "aksha/agents/demo/agent.py",
            "from aksha.runtime.tools import registry\n",
            "agents import aksha.runtime.api and named extensions only",
        ),
        (
            "aksha/agents/demo/agent.py",
            "from aksha.adapters.storage import sqlite\n",
            "agents import aksha.runtime.api and named extensions only",
        ),
        (
            "aksha/extensions/memory/ext.py",
            "from aksha.runtime.artifacts import store\n",
            "extensions import aksha.runtime.api only",
        ),
        (
            "aksha/interfaces/cli/leak.py",
            "from aksha.adapters.storage import postgres\n",
            "interfaces reach adapters, agents and extensions through aksha.app only",
        ),
        (
            "aksha/evals/leak.py",
            "from aksha.adapters.storage import sqlite\n",
            "evals import the runtime and themselves; stores come through aksha.app",
        ),
        (
            "aksha/evals/leak.py",
            "import aksha.app\n",
            "evals import the runtime and themselves; stores come through aksha.app",
        ),
        (
            "aksha/evals/criteria/leak.py",
            "from aksha.agents import research\n",
            "evals import the runtime and themselves; stores come through aksha.app",
        ),
        (
            "aksha/evals/agents/leak.py",
            "from aksha.adapters.clock import fake\n",
            "evals import the runtime and themselves; stores come through aksha.app",
        ),
    ],
)
def test_dependency_rule_catches_a_plant(copy: Path, rel: str, source: str, rule: str) -> None:
    _plant(copy, rel, source)
    found = check_dependency_rule(copy)
    assert [v.rule for v in found] == [rule]
    assert found[0].module == module_name(copy, copy / rel)


@pytest.mark.parametrize(
    ("rel", "source"),
    [
        ("aksha/agents/demo/agent.py", "from aksha.runtime import api\n"),
        ("aksha/agents/demo/agent.py", "from aksha.extensions.memory import ext\n"),
        ("aksha/agents/demo/agent.py", "from . import prompts\n"),
        ("aksha/extensions/memory/ext.py", "from aksha.runtime.api import *\n"),
        ("aksha/adapters/storage/extra.py", "from aksha.runtime.storage.ports import *\n"),
        ("aksha/adapters/storage/extra.py", "from .sqlite import *\nimport psycopg\n"),
        ("aksha/interfaces/cli/extra.py", "from aksha import app\n"),
        ("aksha/interfaces/demo/extra.py", "from aksha import data\n"),
        ("aksha/app.py", '"""Root."""\nfrom aksha.adapters.storage import sqlite\n'),
        ("aksha/runtime/tools/extra.py", "import jsonschema\nimport sqlite3\n"),
    ],
)
def test_dependency_rule_allows_the_permitted_edges(copy: Path, rel: str, source: str) -> None:
    _plant(copy, "aksha/agents/demo/prompts.py", '"""Prompts."""\n')
    _plant(copy, "aksha/extensions/memory/ext.py", '"""Ext."""\n')
    _plant(copy, rel, source)
    assert [str(v) for v in check_dependency_rule(copy)] == []


def test_protocol_guard_catches_a_protocol_outside_ports(copy: Path) -> None:
    _plant(
        copy,
        "aksha/runtime/tools/registry.py",
        '"""Planted."""\nimport typing\nclass Thing(typing.Protocol):\n    pass\n',
    )
    _plant(
        copy,
        "aksha/runtime/tools/ports.py",
        '"""Allowed."""\nfrom typing import Protocol\nclass Fine(Protocol):\n    pass\n',
    )
    assert protocols_outside_ports(copy) == [
        Violation("aksha.runtime.tools.registry", "Thing", "Protocols live in ports.py")
    ]


def test_doc_guard_catches_an_unlisted_module(copy: Path) -> None:
    _plant(copy, "aksha/runtime/tools/unlisted.py", '"""Planted."""\n')
    assert modules_missing_from_doc(copy, DOC) == ["src/aksha/runtime/tools/unlisted.py"]


def test_imports_of_resolves_relative_and_from_imports(copy: Path) -> None:
    src = "from . import registry\nfrom ..events.catalogue import Catalogue\nimport json\n"
    _plant(copy, "aksha/runtime/tools/probe.py", src)
    modules = frozenset({"aksha.runtime.tools.registry", "aksha.runtime.events.catalogue"})
    found = imports_of(copy, copy / "aksha/runtime/tools/probe.py", modules)
    assert found == ["aksha.runtime.tools.registry", "aksha.runtime.events.catalogue", "json"]


def test_tree_paths_rebuilds_paths_from_the_tree() -> None:
    text = (
        "```text\n"
        "aksha/\n"
        "├── app.py              root\n"
        "└── runtime/            domain\n"
        "    ├── api.py          api\n"
        "    └── events/         P01\n"
        "        └── envelope.py envelope\n"
        "```\n"
    )
    assert tree_paths(text) == {
        "aksha/app.py",
        "aksha/runtime/api.py",
        "aksha/runtime/events/envelope.py",
    }
