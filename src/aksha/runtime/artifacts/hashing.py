"""The three hashes of a version (content, canonical, reads) and the entry hash of a path.

ADR-P04 D5. ``content_hash`` (the full content, the chain's encoding) answers "is this the
exact document a person approved?". ``canonical_hash`` (a canonical form) answers "did a
regeneration change anything a reader cares about?". ``reads_hash`` (the sorted lineage
inputs) answers "was it built from the same inputs?". All pure.

The canonical form, in order:

1. The kind's ``canonicalize`` function, if it declares one (pure, for normalisation the
   rules below cannot express).
2. Each pointer in ``canon_exclude`` removed.
3. Each array named in ``list_keys`` sorted by its items' key field.
4. Every string that is a whole artifact ref (``kind/key@vN``) reduced to its slot
   (``kind/key``), and every excerpt (an object holding ``source`` and ``quote``) reduced
   to the source's kind and id and the quote: a moved record or a new ref version with
   the same words is the same claim.
5. The result encoded like the chain (RFC 8785 subset) and hashed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from aksha.runtime.artifacts.types import ArtifactKind, ArtifactRef, InputRef, sorted_inputs
from aksha.runtime.canonical import canonical_bytes, sha256_ref, thaw
from aksha.runtime.storage.chain import content_hash as _chain_content_hash


def content_hash(content: Mapping[str, Any]) -> str:
    """``sha256:`` over the content's canonical bytes; the hash ``artifact.saved`` carries."""
    return _chain_content_hash(content)


def pointer_parts(pointer: str) -> list[str]:
    """The unescaped parts of a JSON pointer (RFC 6901); ``""`` is the whole document."""
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"a JSON pointer starts with '/': {pointer!r}")
    return [p.replace("~1", "/").replace("~0", "~") for p in pointer[1:].split("/")]


def resolve(content: Any, pointer: str) -> tuple[bool, Any]:
    """``(True, value)`` at ``pointer`` in ``content``, or ``(False, None)``."""
    value = content
    for part in pointer_parts(pointer):
        found, value = _child(value, part)
        if not found:
            return False, None
    return True, value


def entry_hash(content: Mapping[str, Any], pointer: str) -> str:
    """The content hash of the entry at ``pointer``: what a path-scoped read pins (S3).

    ``KeyError`` when the version has no such entry.
    """
    found, value = resolve(content, pointer)
    if not found:
        raise KeyError(pointer)
    return sha256_ref(canonical_bytes(value))


def _child(value: Any, part: str) -> tuple[bool, Any]:
    if isinstance(value, Mapping) and part in value:
        return True, value[part]
    if isinstance(value, list | tuple) and part.isdigit() and int(part) < len(value):
        return True, value[int(part)]
    return False, None


def _remove(work: Any, pointer: str) -> None:
    parts = pointer_parts(pointer)
    if not parts:
        return
    parent = work
    for part in parts[:-1]:
        found, parent = _child(parent, part)
        if not found:
            return
    last = parts[-1]
    if isinstance(parent, dict):
        parent.pop(last, None)
    elif isinstance(parent, list) and last.isdigit() and int(last) < len(parent):
        parent.pop(int(last))


def _item_key(key: str) -> Any:
    def by(item: Any) -> bytes:
        return canonical_bytes(item.get(key) if isinstance(item, dict) else item)

    return by


def _sort_lists(work: Any, list_keys: Mapping[str, str]) -> None:
    for pointer, key in sorted(list_keys.items()):
        found, value = resolve(work, pointer)
        if found and isinstance(value, list):
            value.sort(key=_item_key(key))


def _is_excerpt(value: Mapping[str, Any]) -> bool:
    source = value.get("source")
    return "quote" in value and isinstance(source, Mapping) and "id" in source


def _reduce(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return ArtifactRef.parse(value).slot_id()
        except ValueError:
            return value
    if isinstance(value, dict):
        if _is_excerpt(value):
            src = value["source"]
            return {"source": {"kind": src.get("kind"), "id": src["id"]}, "quote": value["quote"]}
        return {k: _reduce(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_reduce(v) for v in value]
    return value


def canonical_form(kind: ArtifactKind, content: Mapping[str, Any]) -> Any:
    """The canonical form of ``content`` under ``kind``'s rules (module doc, steps 1 to 4)."""
    base = kind.canonicalize(content) if kind.canonicalize is not None else content
    work = thaw(base)
    for pointer in kind.canon_exclude:
        _remove(work, pointer)
    _sort_lists(work, kind.list_keys)
    return _reduce(work)


def canonical_hash(kind: ArtifactKind, content: Mapping[str, Any]) -> str:
    """``sha256:`` over the canonical form's bytes (ADR-P04 D5)."""
    return sha256_ref(canonical_bytes(canonical_form(kind, content)))


def reads_hash(reads: Iterable[InputRef]) -> str:
    """``sha256:`` over the sorted, deduplicated lineage inputs."""
    return sha256_ref(canonical_bytes([r.as_json() for r in sorted_inputs(reads)]))
