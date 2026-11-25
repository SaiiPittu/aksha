from pathlib import Path

import pytest

from aksha.runtime.definitions.hashing import (
    definition_hash,
    file_hashes,
    resume_refusal,
    tree_hash,
)
from aksha.runtime.definitions.loader import load_agents
from tests.support.agents import copy_plugins


def _folder(tmp_path: Path) -> Path:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "agent.py").write_text("x = 1\n")
    (tmp_path / "a" / "prompts").mkdir()
    (tmp_path / "a" / "prompts" / "p.md").write_text("hello\n")
    (tmp_path / "a" / "tests").mkdir()
    (tmp_path / "a" / "tests" / "t.py").write_text("ignored\n")
    (tmp_path / "a" / "__pycache__").mkdir()
    (tmp_path / "a" / "__pycache__" / "agent.cpython-312.pyc").write_bytes(b"\0")
    return tmp_path / "a"


def test_file_hashes_skip_tests_and_bytecode(tmp_path: Path) -> None:
    hashes = file_hashes(_folder(tmp_path))
    assert [p for p, _ in hashes] == ["agent.py", "prompts/p.md"]
    assert all(len(h) == 64 for _, h in hashes)


def test_tree_hash_changes_with_any_byte(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    before = tree_hash(file_hashes(folder))
    (folder / "prompts" / "p.md").write_text("hello!\n")
    assert tree_hash(file_hashes(folder)) != before


def test_definition_hash_covers_files_extensions_and_api_version(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    base = definition_hash(folder, [], "1.0", tools_hash="sha256:t")
    assert base.startswith("sha256:") and len(base) == 7 + 64
    assert definition_hash(folder, [], "1.1", tools_hash="sha256:t") != base
    (folder / "tests" / "t.py").write_text("changed\n")
    assert definition_hash(folder, [], "1.0", tools_hash="sha256:t") == base


@pytest.mark.parametrize(
    ("recorded", "expected"),
    [("sha256:a", None), ("sha256:b", "definition_changed"), (None, "definition_changed")],
)
def test_resume_refusal(recorded: str | None, expected: str | None) -> None:
    assert resume_refusal(recorded, "sha256:a") == expected


def test_registry_hash_in_definition_hash(tmp_path: Path) -> None:
    """Changing one tool's schema changes the definition hash; a resumed run is refused."""
    folder = _folder(tmp_path)
    assert definition_hash(folder, [], "1.0", tools_hash="sha256:a") != definition_hash(
        folder, [], "1.0", tools_hash="sha256:b"
    )
    plugins = copy_plugins(tmp_path / "plugins")
    (before,) = load_agents([plugins.toy], [plugins.extensions])
    plugins.edit(
        "extensions/echo/__init__.py",
        '"properties": {"text": {"type": "string"}}',
        '"properties": {"text": {"type": "string", "maxLength": 100}}',
    )
    (after,) = load_agents([plugins.toy], [plugins.extensions])
    assert after.registry.canonical_hash() != before.registry.canonical_hash()
    assert resume_refusal(before.definition_hash, after.definition_hash) == "definition_changed"
