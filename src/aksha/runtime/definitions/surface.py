"""``api_surface``: every public name of :mod:`aksha.runtime.api` with its signature (D2).

The snapshot test compares it with ``tests/architecture/api_surface.json``, so the
extension API cannot change by accident. ``required_bump`` says which part of
``API_VERSION`` a change needs: a new name, or new trailing parameters with defaults, is
minor; any other change, or a removed name, is major.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
from collections.abc import Mapping
from typing import Literal

Bump = Literal["none", "minor", "major"]


def api_surface() -> Mapping[str, str]:
    """Public name to signature text, for every name in ``aksha.runtime.api.__all__``."""
    from aksha.runtime import api  # deferred: api imports this package

    return {
        name: _describe(name, getattr(api, name))
        for name in sorted(api.__all__)
        if name != "API_VERSION"
    }


def _describe(name: str, obj: object) -> str:
    if inspect.isclass(obj):
        if dataclasses.is_dataclass(obj):
            return f"class {name}{_stable_signature(inspect.signature(obj))}"
        members = sorted(m for m in vars(obj) if not m.startswith("_"))
        parts = [f"{m}{_signature(getattr(obj, m))}" for m in members]
        return f"class {name}: {'; '.join(parts)}"
    if inspect.isfunction(obj):
        return f"def {name}{_stable_signature(inspect.signature(obj))}"
    return f"{name} = {obj!r}"


class _Shown:
    """A default value whose ``repr`` is :func:`stable_repr`, for printing a signature."""

    def __init__(self, value: object) -> None:
        self.value = value

    def __repr__(self) -> str:
        return stable_repr(self.value)


def _stable_signature(sig: inspect.Signature) -> inspect.Signature:
    params = [
        p if p.default is inspect.Parameter.empty else p.replace(default=_Shown(p.default))
        for p in sig.parameters.values()
    ]
    return sig.replace(parameters=params)


def stable_repr(value: object) -> str:
    """``repr`` with set members sorted, so the text never depends on hash randomisation."""
    if isinstance(value, frozenset | set):
        inner = ", ".join(sorted(stable_repr(v) for v in value))
        return f"{type(value).__name__}({{{inner}}})" if value else f"{type(value).__name__}()"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        args = ", ".join(
            f"{f.name}={stable_repr(getattr(value, f.name))}" for f in dataclasses.fields(value)
        )
        return f"{type(value).__name__}({args})"
    if isinstance(value, tuple):
        inner = ", ".join(stable_repr(v) for v in value)
        return f"({inner},)" if len(value) == 1 else f"({inner})"
    return repr(value)


def _signature(member: object) -> str:
    if isinstance(member, property) and member.fget is not None:
        return f" (property) {inspect.signature(member.fget)}"
    if inspect.isfunction(member):
        return str(inspect.signature(member))
    return ""


def required_bump(old: Mapping[str, str], new: Mapping[str, str]) -> Bump:
    """Which part of ``API_VERSION`` a change from ``old`` to ``new`` needs.

    ``major`` if a name was removed or its signature changed in a way an existing caller
    could notice; ``minor`` if a name was added, or a signature only gained trailing
    parameters that all have defaults (a new optional ``Extension`` field); else ``none``.
    """
    minor = bool(set(new) - set(old))
    for name, text in old.items():
        if name not in new:
            return "major"
        if new[name] == text:
            continue
        if not only_added_defaults(text, new[name]):
            return "major"
        minor = True
    return "minor" if minor else "none"


_PARAM = re.compile(r", (?=\*?\w+: ')")


def only_added_defaults(old: str, new: str) -> bool:
    """Whether ``new`` is ``old`` plus trailing parameters that each have a default."""
    if ")" not in old or ")" not in new:
        return False
    old_head, old_tail = old.rsplit(")", 1)
    new_head, new_tail = new.rsplit(")", 1)
    if old_tail != new_tail or not new_head.startswith(old_head):
        return False
    added = new_head[len(old_head) :]
    if not added.startswith(", "):
        return False
    return all(" = " in part for part in _PARAM.split(added[2:]))
