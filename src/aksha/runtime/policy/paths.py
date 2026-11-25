"""Workspace paths for the two path matchers: normal form, and whether one lies under a root.

A path argument and a rule's root are compared only in normal form: relative, POSIX,
no ``.`` or ``..`` segment, no trailing slash. ``normalised_path`` gives that form, or
None for a path it cannot place (empty, absolute, a backslash, or one that climbs out of
the workspace), and both matchers fail closed on None.

The two matchers compare case differently, each in the direction that refuses more:
``path_under`` (an allow-list) is case-sensitive, so ``SRC/x`` is not under ``src``;
``path_not_under`` (a deny-list) ignores case, so ``.SECRET/key`` is still under ``.secret``
on a filesystem that folds case.
"""

from __future__ import annotations

import posixpath

WORKSPACE = "."
"""The normal form of the workspace root itself: every placeable path lies under it."""


def normalised_path(raw: str) -> str | None:
    """``raw`` as a clean relative POSIX path, or None if empty, absolute or escaping."""
    if not raw or raw.startswith("/") or "\\" in raw:
        return None
    clean = posixpath.normpath(raw)
    if clean == ".." or clean.startswith("../"):
        return None
    return clean


def root_problem(root: object) -> str | None:
    """Why ``root`` cannot be a rule's path root, or None when it is already in normal form."""
    if not isinstance(root, str):
        return f"a path root is a string, not {type(root).__name__}"
    clean = normalised_path(root)
    if clean is None:
        return f"path root {root!r} is empty, absolute or climbs out of the workspace"
    if clean != root:
        return f"path root {root!r} is not in normal form; write {clean!r}"
    return None


def is_under(path: str, root: str, *, fold_case: bool) -> bool:
    """Whether the normal-form ``path`` is ``root`` or lies inside it.

    ``root`` is normalised here as well, so a root written as ``./.secret`` or ``.secret/``
    still matches; a root that cannot be placed matches nothing.
    """
    clean_root = normalised_path(root)
    if clean_root is None:
        return False
    if clean_root == WORKSPACE:
        return True
    if fold_case:
        path, clean_root = path.casefold(), clean_root.casefold()
    return path == clean_root or path.startswith(clean_root + "/")
