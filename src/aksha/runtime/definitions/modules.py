"""Import an agent or extension folder fresh, and scan its sources before trusting them.

Loading runs the module's code, so only trusted folders load (D9). Each load purges the
previous copy from ``sys.modules`` first, so an edit between two loads is always seen.
``scan_sources`` reads the files without running them and reports two things the loader
refuses:

1. An import of any ``aksha`` module other than :mod:`aksha.runtime.api` (D1). That covers
   ``import aksha.adapters...``, ``from aksha import runtime`` (read as ``aksha.runtime``),
   ``from aksha.app import ...`` and ``importlib.import_module("aksha....")`` with a literal
   name. An agent may also import ``aksha.extensions.<name>`` for each extension it names;
   the loader allows those once it knows the names (:attr:`SourceFinding.target`).
2. A class shaped like a stop policy (decision 10: the set is closed).

The scan is a guard against mistakes, not a sandbox: a module built to hide an import
(``getattr`` chains, ``exec``) is beyond it, which is why only trusted folders load (D9).
"""

from __future__ import annotations

import ast
import importlib
import importlib.machinery
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

_API = "aksha.runtime.api"
_EXTENSIONS = "aksha.extensions"
_DYNAMIC_IMPORTS = frozenset({"import_module", "__import__"})
_STOP_POLICY_BASES = frozenset({"StopPolicy", "Director"})
_STOP_POLICY_METHODS = frozenset({"prepare", "judge"})


@dataclass(frozen=True)
class SourceFinding:
    """One refused construct in a source file, located by relative path and line."""

    file: str
    line: int
    code: str
    message: str
    target: str | None = None
    """The imported module, for an ``import_outside_api`` finding."""

    def named_extension(self) -> str | None:
        """``echo`` when the finding is an import of ``aksha.extensions.echo`` (or inside it)."""
        if self.target is None or not self.target.startswith(_EXTENSIONS + "."):
            return None
        return self.target.split(".")[2]


def import_value(folder: Path, qualname: str, module: str | None, attr: str) -> object | None:
    """Import ``folder`` as package ``qualname`` and return ``attr`` of ``module`` in it.

    ``module`` None means the package's ``__init__.py`` itself. Returns None when the
    attribute is absent; import errors propagate to the caller, which turns them into a
    refusal.
    """
    for name in [n for n in sys.modules if n == qualname or n.startswith(qualname + ".")]:
        del sys.modules[name]
    importlib.invalidate_caches()
    init = folder / "__init__.py"
    if init.exists():
        spec = importlib.util.spec_from_file_location(
            qualname, init, submodule_search_locations=[str(folder)]
        )
    else:  # a folder with no __init__.py loads as a namespace package
        spec = importlib.machinery.ModuleSpec(qualname, None, is_package=True)
        spec.submodule_search_locations = [str(folder)]
    if spec is None:
        raise ImportError(f"cannot import {folder}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = package
    if spec.loader is not None:
        spec.loader.exec_module(package)
    target = package if module is None else importlib.import_module(f"{qualname}.{module}")
    return getattr(target, attr, None)


def scan_sources(folder: Path) -> tuple[SourceFinding, ...]:
    """Refused imports and stop-policy-shaped classes in ``folder``'s Python files.

    A top-level ``tests/`` folder is skipped: it is not part of the definition.
    """
    out: list[SourceFinding] = []
    for path in sorted(folder.rglob("*.py")):
        rel = path.relative_to(folder)
        if rel.parts[0] == "tests" or "__pycache__" in rel.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        out.extend(_findings(rel.as_posix(), tree))
    return tuple(out)


def _findings(file: str, tree: ast.Module) -> list[SourceFinding]:
    out: list[SourceFinding] = []
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.Import | ast.ImportFrom):
            targets = _imported(node)
        elif isinstance(node, ast.Call):
            targets = _dynamic_import(node)
        for target in targets:
            if _is_aksha(target) and not _is_api(target):
                message = f"imports {target}; import aksha.runtime.api only"
                line = getattr(node, "lineno", 0)
                out.append(SourceFinding(file, line, "import_outside_api", message, target))
        if isinstance(node, ast.ClassDef) and _is_stop_policy(node):
            message = f"class {node.name} is a stop policy; opt into a built-in one instead"
            out.append(SourceFinding(file, node.lineno, "stop_policy_class", message))
    return out


def _is_aksha(target: str) -> bool:
    return target == "aksha" or target.startswith("aksha.")


def _is_api(target: str) -> bool:
    return target == _API or target.startswith(_API + ".")


def _imported(node: ast.Import | ast.ImportFrom) -> list[str]:
    """The modules (or names) an import statement reaches, absolute.

    ``from aksha.x import y`` is read as ``aksha.x.y``, so ``from aksha import runtime``
    is ``aksha.runtime`` and ``from aksha.runtime.api import Agent`` stays inside the API.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level or not node.module:
        return []  # a relative import stays inside the folder
    if _is_aksha(node.module):
        return [f"{node.module}.{a.name}" for a in node.names]
    return [node.module]


def _dynamic_import(call: ast.Call) -> list[str]:
    """``importlib.import_module("aksha.app")`` or ``__import__("aksha.app")``, by literal name."""
    fn = call.func
    name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
    if name not in _DYNAMIC_IMPORTS or not call.args:
        return []
    first = call.args[0]
    return [first.value] if isinstance(first, ast.Constant) and isinstance(first.value, str) else []


def _is_stop_policy(cls: ast.ClassDef) -> bool:
    bases = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in cls.bases}
    methods = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
    return bool(bases & _STOP_POLICY_BASES) or methods >= _STOP_POLICY_METHODS
