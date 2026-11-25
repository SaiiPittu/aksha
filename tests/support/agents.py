"""Copy the fixture agent and extension into a temporary folder, and edit them there.

The loader tests never load the fixtures in place: each test gets its own copy, so an
edit (a prompt, one byte of an extension) cannot leak into another test.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@dataclass(frozen=True)
class Plugins:
    """A temporary agent search path and extension search path with the fixtures copied in."""

    agents: Path
    extensions: Path

    @property
    def toy(self) -> Path:
        """The copied conformance agent's folder."""
        return self.agents / "toy"

    @property
    def echo(self) -> Path:
        """The copied conformance extension's folder."""
        return self.extensions / "echo"

    def edit(self, rel: str, old: str, new: str) -> None:
        """Replace ``old`` with ``new`` once in ``rel``, a path relative to the temporary folder."""
        path = self.agents.parent / rel
        text = path.read_text(encoding="utf-8")
        assert old in text, f"{old!r} not in {rel}"
        path.write_text(text.replace(old, new, 1), encoding="utf-8")


def copy_plugins(tmp: Path) -> Plugins:
    """Copy ``toy`` into ``tmp/agents`` and ``echo`` into ``tmp/extensions``."""
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(FIXTURES / "agents" / "toy", tmp / "agents" / "toy", ignore=ignore)
    shutil.copytree(FIXTURES / "extensions" / "echo", tmp / "extensions" / "echo", ignore=ignore)
    return Plugins(tmp / "agents", tmp / "extensions")
