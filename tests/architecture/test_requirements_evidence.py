"""The requirements matrix cannot rot: every path and pytest node ID it names exists.

``docs/requirements.json`` holds the rows of ``docs/requirements.md``. Each evidence item
is a ``path`` (a file or folder in the repository), a ``test`` (a pytest node ID, checked
by parsing the test module, so this test collects nothing) or a ``command`` (prose, not
checked here). The Markdown page must list the same row IDs as the JSON.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "docs" / "requirements.json"
PAGE = ROOT / "docs" / "requirements.md"
STATUSES = {"met", "partly met", "not met"}


def _rows() -> list[dict[str, Any]]:
    doc = json.loads(MATRIX.read_text(encoding="utf-8"))
    return [row for group in doc["groups"] for row in group["rows"]]


def _names(module: Path) -> set[str]:
    """Every test function in ``module``, as ``name`` or ``Class::name``."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.add(node.name)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.add(f"{node.name}::{item.name}")
    return found


def missing_evidence(rows: list[dict[str, Any]], root: Path = ROOT) -> list[str]:
    """Each path or node ID in ``rows`` that does not exist under ``root``."""
    missing: list[str] = []
    for row in rows:
        for item in row["evidence"]:
            ref = item["ref"]
            if item["kind"] == "path":
                if not (root / ref).exists():
                    missing.append(f"{row['id']}: path {ref}")
            elif item["kind"] == "test":
                file, _, name = ref.partition("::")
                module = root / file
                if not module.is_file() or re.sub(r"\[.*\]$", "", name) not in _names(module):
                    missing.append(f"{row['id']}: test {ref}")
    return missing


def test_every_path_and_node_id_exists() -> None:
    assert missing_evidence(_rows()) == []


def test_the_guard_catches_a_planted_path_and_node_id() -> None:
    planted = [{"id": "R-99", "evidence": [
        {"kind": "path", "ref": "docs/no-such-file.md"},
        {"kind": "test", "ref": "tests/architecture/test_requirements_evidence.py::test_nothing"},
        {"kind": "test", "ref": "tests/no_such_module.py::test_nothing"},
    ]}]  # fmt: skip
    assert len(missing_evidence(planted)) == 3


def test_rows_have_ids_statuses_and_reasons() -> None:
    rows = _rows()
    ids = [row["id"] for row in rows]
    assert ids == [f"R-{n:02d}" for n in range(1, len(rows) + 1)]
    for row in rows:
        assert row["status"] in STATUSES, row["id"]
        assert row["evidence"], row["id"]
        assert (row["status"] != "met") == bool(row.get("reason")), row["id"]


def test_the_page_lists_the_same_rows() -> None:
    page = PAGE.read_text(encoding="utf-8")
    on_page = re.findall(r"^\| (R-\d\d) \|", page, flags=re.MULTILINE)
    assert on_page == [row["id"] for row in _rows()]
