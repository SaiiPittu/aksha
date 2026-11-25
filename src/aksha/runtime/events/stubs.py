"""Blob stubs: what an event keeps in place of a value moved to the blob store.

A payload over the 64 KiB cap (ADR-P01 D7) has its largest values moved to the blob store
(``aksha.runtime.artifacts.blobs.offload``); each moved value is replaced by a stub of
exactly three keys, ``{"blob": "sha256:<hex>", "bytes": <n>, "type": <media type>}``
(maintainer's decision 25, 26 Nov 2026). The event's hash covers the stub, and the stub
names the blob by its content hash, so an edited blob is found like an edited row
(ADR-P01 H6, the blob half).

The catalogue reads this module to skip schema checks at a stub: the payload was checked
whole before its values were moved, and the stored stub cannot be checked again without
the blob store.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

BLOB_STUB_KEYS: frozenset[str] = frozenset({"blob", "bytes", "type"})
"""The keys of a stub, and no others."""

BLOB_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
"""A blob's name: ``sha256:`` and the 64 lowercase hex characters of its bytes' SHA-256."""

TEXT_TYPE = "text/plain; charset=utf-8"
"""The media type of a moved string: its UTF-8 bytes."""

JSON_TYPE = "application/json"
"""The media type of a moved object or array: its canonical bytes."""

Path = tuple[str | int, ...]
"""A location in a payload: object keys and array indexes from the root."""


def is_blob_stub(value: Any) -> bool:
    """Whether ``value`` is a stub: exactly the three keys, a blob ref, a size, a type."""
    if not isinstance(value, Mapping) or set(value) != BLOB_STUB_KEYS:
        return False
    blob, size, media = value["blob"], value["bytes"], value["type"]
    return (
        isinstance(blob, str)
        and BLOB_REF.match(blob) is not None
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size >= 0
        and media in (TEXT_TYPE, JSON_TYPE)
    )


def stub_paths(payload: Any) -> tuple[Path, ...]:
    """The path of every stub in ``payload``, in document order; never the root itself."""
    return tuple(_walk(payload, ()))


def _walk(value: Any, path: Path) -> Iterator[Path]:
    if path and is_blob_stub(value):
        yield path
        return
    if isinstance(value, Mapping):
        for k in sorted(value):
            yield from _walk(value[k], (*path, k))
    elif isinstance(value, list | tuple):
        for i, item in enumerate(value):
            yield from _walk(item, (*path, i))


def under(path: Path, prefixes: tuple[Path, ...]) -> bool:
    """Whether ``path`` is one of ``prefixes`` or lies inside one."""
    return any(path[: len(p)] == p for p in prefixes)
