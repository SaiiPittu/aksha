"""Namespacing: how a local name becomes a full name, and how a reference is resolved (D6).

Every kind, relation, projection, alias and own tool is declared with a local name and
known to the runtime by its full name, ``<agent or extension>.<local>``. A reference with a
dot is already a full name; one without is local to whoever wrote it; ``record:<name>``
names a source collection, not a kind.
"""

from __future__ import annotations

import re

_LOCAL = re.compile(r"^[a-z][a-z0-9_]*$")


def is_local_name(name: str) -> bool:
    """True for a lower-case identifier with no dot: ``line``, ``save_line``."""
    return bool(_LOCAL.match(name))


def qualify(namespace: str, local: str) -> str:
    """The full name of ``local`` declared by ``namespace``: ``notes.line``."""
    return f"{namespace}.{local}"


def resolve_kind(ref: str, namespace: str) -> str:
    """What ``ref`` means when ``namespace`` wrote it; records and full names pass as is."""
    if ref.startswith("record:") or "." in ref:
        return ref
    return qualify(namespace, ref)
