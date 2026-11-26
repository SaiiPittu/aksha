"""Installed from a wheel, nothing under src/ may reach outside the package for a file.

``Path(__file__).parents[n]`` past the package root finds a checkout's folders, which do not
exist under site-packages. Bundled files are found through :mod:`aksha.data` instead. The
one exemption reads the checkout's git commit and copes with there being none.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "src" / "aksha"
EXEMPT = {("aksha/app.py", 2)}
"""``runtime_commit``: the checkout's git commit, ``unknown`` when installed."""


def escapes(root: Path) -> list[tuple[str, int]]:
    """Every ``.parents[n]`` whose ``n`` climbs above ``root``'s package folder."""
    found: list[tuple[str, int]] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root.parent).as_posix()
        depth = len(path.relative_to(root).parts) - 1  # folders between the file and root
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "parents"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, int)
                and node.slice.value > depth
            ):
                found.append((rel, node.slice.value))
    return found


def test_nothing_in_the_package_climbs_out_of_it() -> None:
    assert set(escapes(PACKAGE)) - EXEMPT == set()


def test_the_exemption_is_still_needed() -> None:
    assert set(escapes(PACKAGE)) == EXEMPT


def test_a_planted_climb_is_caught(tmp_path: Path) -> None:
    pkg = tmp_path / "aksha" / "sub"
    pkg.mkdir(parents=True)
    (pkg / "m.py").write_text(
        "from pathlib import Path\nA = Path(__file__).parents[1]\nB = Path(__file__).parents[2]\n",
        encoding="utf-8",
    )
    assert escapes(tmp_path / "aksha") == [("aksha/sub/m.py", 2)]
