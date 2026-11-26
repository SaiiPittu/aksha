"""The field-level redline of a diff review (ADR-P05 D1, AE-8): what moved, as tracked changes.

``redline`` walks the old and the new content together. Objects are compared key by key;
a list the kind keys (``ArtifactKind.list_keys``, a pointer to the field its items are
matched by) is compared row by row on that field, so a row that moved position is not a
change and the pointer names the row's key; any other list is compared by position.
Leaves that differ, and keys or rows only one side has, are hunks. At most ``max_hunks``
are returned, in pointer order. Word-level redlines and quote columns are later work.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.artifacts.types import ArtifactKind
from aksha.runtime.invalidation.types import Hunk

MAX_HUNKS = 40
"""The redline's cap (AE-8 lean: field level, capped at 40 hunks)."""


def _escape(part: str | int) -> str:
    return str(part).replace("~", "~0").replace("/", "~1")


def redline(
    kind: ArtifactKind,
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    *,
    max_hunks: int = MAX_HUNKS,
) -> tuple[Hunk, ...]:
    """The field-level hunks from ``old`` to ``new`` (module doc). Pure."""
    keyed = {_pattern(p): f for p, f in kind.list_keys.items()}
    out: list[Hunk] = []
    _walk(old, new, "", "", keyed, out)
    return tuple(sorted(out, key=lambda h: h.path)[:max_hunks])


def _pattern(pointer: str) -> str:
    """A ``list_keys`` pointer with every index replaced by ``*``, as the walk names paths."""
    return "/".join("*" if p.isdigit() else p for p in pointer.split("/"))


def _walk(
    old: Any, new: Any, path: str, shape: str, keyed: Mapping[str, str], out: list[Hunk]
) -> None:
    if isinstance(old, Mapping) and isinstance(new, Mapping):
        for k in sorted(set(old) | set(new)):
            sub, sub_shape = f"{path}/{_escape(k)}", f"{shape}/{_escape(k)}"
            if k not in old:
                out.append(Hunk(sub, None, new[k]))
            elif k not in new:
                out.append(Hunk(sub, old[k], None))
            else:
                _walk(old[k], new[k], sub, sub_shape, keyed, out)
        return
    if _is_list(old) and _is_list(new):
        field = keyed.get(shape)
        if field is not None and _all_keyed(old, field) and _all_keyed(new, field):
            _walk_keyed(old, new, path, shape, field, keyed, out)
            return
        for i in range(max(len(old), len(new))):
            sub, sub_shape = f"{path}/{i}", f"{shape}/*"
            if i >= len(old):
                out.append(Hunk(sub, None, new[i]))
            elif i >= len(new):
                out.append(Hunk(sub, old[i], None))
            else:
                _walk(old[i], new[i], sub, sub_shape, keyed, out)
        return
    if old != new:
        out.append(Hunk(path, old, new))


def _walk_keyed(
    old: Any, new: Any, path: str, shape: str, field: str, keyed: Mapping[str, str],
    out: list[Hunk],
) -> None:  # fmt: skip
    before = {str(item[field]): item for item in old}
    after = {str(item[field]): item for item in new}
    for k in sorted(set(before) | set(after)):
        sub = f"{path}/{_escape(k)}"
        if k not in before:
            out.append(Hunk(sub, None, after[k]))
        elif k not in after:
            out.append(Hunk(sub, before[k], None))
        else:
            _walk(before[k], after[k], sub, f"{shape}/*", keyed, out)


def _is_list(value: Any) -> bool:
    return isinstance(value, list | tuple)


def _all_keyed(items: Any, field: str) -> bool:
    return all(isinstance(i, Mapping) and field in i for i in items)
