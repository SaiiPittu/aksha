"""The bundled synthetic worlds and eval suites, found inside the installed package.

``worlds/<name>/`` holds a world (``sources/`` is what a person uploads, ``web/`` the
pages its offline web serves, beside its manifest, its ``web.json`` and its answer key),
``suites/<suite>/`` an eval suite and ``recordings/<name>/`` a recorded run. They ship inside the
wheel, so ``aksha demo`` and ``aksha eval`` work from ``pip install`` with no clone. This
module is the one place a world or a suite is found; nothing writes here (the demo copies
a world's ``sources/`` into its work folder first).
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def root() -> Path:
    """The folder holding ``worlds/`` and ``suites/`` (the evals' fixtures root)."""
    return Path(str(files(__name__)))


def worlds() -> Path:
    """The folder of bundled worlds."""
    return root() / "worlds"


def suites() -> Path:
    """The folder of bundled eval suites."""
    return root() / "suites"


def recordings() -> Path:
    """The folder of bundled recordings (``aksha replay`` runs one again offline)."""
    return root() / "recordings"


def world_path(name: str) -> Path:
    """The bundled world ``name``; raises ``KeyError`` naming the worlds there are."""
    path = worlds() / name
    if not (path / "sources").is_dir():
        known = sorted(p.name for p in worlds().iterdir() if (p / "sources").is_dir())
        raise KeyError(f"no bundled world {name!r}; there are {known}")
    return path
