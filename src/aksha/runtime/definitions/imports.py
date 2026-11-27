"""What an agent or extension imports from outside its folder, pinned or refused (ADR-P20 B10).

The definition version hash covers the agent folder and each named extension's files, so a
module imported from elsewhere could change behaviour with the hash unchanged, and a run
could resume under changed code (D7). The loader therefore reads every absolute import of a
folder's sources (without running them) and sorts each top-level module into one of four:

1. The standard library, ``__future__`` and ``aksha`` itself: nothing to pin (the import
   rule in ``modules.py`` already limits ``aksha``; the runtime commit is on ``run.started``).
2. A module of an installed distribution: pinned by the distribution's name and version,
   which enter the definition hash. A distribution is versioned code a person installs on
   purpose; a new version is a new definition.
3. A module that is not installed: pinned as absent, so installing it later changes the
   hash (an optional parser an extension imports inside ``try`` is the usual case).
4. Anything else is refused ``import_unpinned``: a module found on ``sys.path`` that no
   distribution owns (a helper package beside the agent, a ``PYTHONPATH`` folder), or an
   editable install, whose version does not follow its code. Such a helper belongs inside
   the agent folder, where its files are hashed, or in an installed package.

Pure apart from reading the import system's metadata: nothing is imported or executed.
"""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.metadata
import importlib.util
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from aksha.runtime.definitions.modules import SourceFinding

ImportPin = tuple[str, str]
"""(top-level module, what pins it): ``"name==version"`` for each installed distribution that
owns the module, comma-separated and sorted (a namespace such as ``opentelemetry`` has
several), ``"absent"`` for a module not installed, or ``"folder"`` for one inside the folder."""

_NOTHING_TO_PIN = frozenset({"aksha", "__future__"})
_SKIPPED_DIRS = frozenset({"__pycache__"})


@dataclass(frozen=True)
class ExternalImport:
    """One absolute import of a module outside the standard library and ``aksha``."""

    file: str
    line: int
    module: str
    """The top-level module name."""


def external_imports(folder: Path) -> tuple[ExternalImport, ...]:
    """Every absolute import in ``folder``'s Python files that may need a pin, in file order.

    A top-level ``tests/`` folder is skipped, as the hash skips it. Relative imports stay
    inside the folder, whose files the hash already covers.
    """
    out: list[ExternalImport] = []
    for path in sorted(folder.rglob("*.py")):
        rel = path.relative_to(folder)
        if rel.parts[0] == "tests" or _SKIPPED_DIRS & set(rel.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".", 1)[0]
                if top in _NOTHING_TO_PIN or top in sys.stdlib_module_names:
                    continue
                out.append(ExternalImport(rel.as_posix(), node.lineno, top))
    return tuple(out)


def pin_imports(
    imports: Iterable[ExternalImport], folder: Path
) -> tuple[tuple[ImportPin, ...], tuple[SourceFinding, ...]]:
    """The sorted pins of ``imports``, and an ``import_unpinned`` finding for each refused one."""
    pins: dict[str, ImportPin] = {}
    refused: list[SourceFinding] = []
    owners = _owners()
    for imp in imports:
        if imp.module in pins:
            continue
        pin, why = _pin(imp.module, folder, owners)
        if pin is None:
            message = (
                f"imports {imp.module}, {why}; the definition hash cannot cover it, so move it "
                "into the agent folder or install it as a package (ADR-P20 B10)"
            )
            refused.append(SourceFinding(imp.file, imp.line, "import_unpinned", message,
                                         imp.module))  # fmt: skip
            continue
        pins[imp.module] = pin
    return tuple(sorted(pins.values(), key=lambda p: p[0])), tuple(refused)


def _pin(
    module: str, folder: Path, owners: Mapping[str, tuple[str, ...]]
) -> tuple[ImportPin | None, str]:
    try:
        spec = importlib.machinery.PathFinder.find_spec(module)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        spec = _builtin_spec(module)
    if spec is None:
        return (module, "absent"), ""
    origin = spec.origin or next(iter(spec.submodule_search_locations or ()), None)
    if origin and Path(origin).resolve().is_relative_to(folder.resolve()):
        return (module, "folder"), ""  # the folder's own file: already hashed
    names = sorted(set(owners.get(module, ())))
    if not names:
        return None, "which no installed distribution owns (a module on sys.path)"
    pinned: list[str] = []
    for name in names:
        dist = importlib.metadata.distribution(name)
        if _editable(dist):
            why = f"from an editable install of {name}, whose version does not follow its code"
            return None, why
        pinned.append(f"{name}=={dist.version}")
    return (module, ",".join(pinned)), ""


def _builtin_spec(module: str) -> importlib.machinery.ModuleSpec | None:
    try:
        return importlib.util.find_spec(module) if module in sys.builtin_module_names else None
    except (ImportError, ValueError):
        return None


@lru_cache(maxsize=1)
def _owners() -> Mapping[str, tuple[str, ...]]:
    return {k: tuple(v) for k, v in importlib.metadata.packages_distributions().items()}


def _editable(dist: importlib.metadata.Distribution) -> bool:
    raw = dist.read_text("direct_url.json")
    if not raw:
        return False
    try:
        info = json.loads(raw)
    except ValueError:
        return False
    return bool(isinstance(info, dict) and (info.get("dir_info") or {}).get("editable"))


def refresh() -> None:
    """Forget the cached distribution owners (a test that installs or removes one)."""
    _owners.cache_clear()
