"""Payload offload to the blob store: ``offload``, ``inflate``, ``blob_problems`` and helpers.

The maintainer's decision 25 (26 Nov 2026), ADR-P01 D7 and ADR-P04 D10: a payload whose
canonical JSON is over 64 KiB keeps its small fields and has its largest values moved to
the blob store, each replaced by a stub ``{"blob": "sha256:...", "bytes": n, "type": t}``
(:mod:`aksha.runtime.events.stubs`). The rules:

1. The whole payload is checked against its event type's schema first, so a stored stub
   always stands for a value that passed.
2. Fields a dedup key reads are never moved, so the key renders the same.
3. The largest string is moved first (its UTF-8 bytes, ``text/plain; charset=utf-8``);
   only when no string over :data:`MIN_OFFLOAD_BYTES` is left is the largest object or
   array moved (its canonical bytes, ``application/json``). Repeat until under the cap.
4. Deterministic: the same payload gives the same stubs, so a replayed step's log is the
   same bytes.

A payload that is still over the cap with nothing left to move is refused
(``payload_too_large``), as it was before offload existed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from aksha.runtime.artifacts.errors import BlobError
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.canonical import canonical_bytes, freeze, thaw
from aksha.runtime.events.catalogue import PAYLOAD_CAP_BYTES, Catalogue
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.events.errors import PayloadTooLarge
from aksha.runtime.events.stubs import BLOB_REF, JSON_TYPE, TEXT_TYPE, Path, is_blob_stub

MIN_OFFLOAD_BYTES = 1024
"""A value smaller than this is never moved: its stub would save almost nothing."""


def check_blob_ref(ref: str) -> str:
    """Return ``ref``, or refuse one that is not ``sha256:`` and 64 hex characters."""
    if not isinstance(ref, str) or BLOB_REF.match(ref) is None:
        raise BlobError("blob_ref_invalid", f"not a blob ref: {ref!r}")
    return ref


def _size(value: Any) -> int:
    return len(canonical_bytes(value))


def _candidates(value: Any, path: Path, keep: frozenset[str]) -> list[tuple[Path, Any]]:
    out: list[tuple[Path, Any]] = []
    if path and is_blob_stub(value):
        return out
    if path:
        out.append((path, value))
    if isinstance(value, dict):
        for k in sorted(value):
            if path or k not in keep:
                out.extend(_candidates(value[k], (*path, k), keep))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            out.extend(_candidates(item, (*path, i), keep))
    return out


def _pick(work: Any, keep: frozenset[str]) -> tuple[Path, Any] | None:
    """The next value to move (rule 3), or None when nothing is worth moving."""
    found = [(p, v, _size(v)) for p, v in _candidates(work, (), keep)]
    found = [f for f in found if f[2] >= MIN_OFFLOAD_BYTES]
    strings = [f for f in found if isinstance(f[1], str)]
    pool = strings or [f for f in found if isinstance(f[1], dict | list)]
    if not pool:
        return None
    path, value, _ = max(pool, key=lambda f: (f[2], tuple(map(str, f[0]))))
    return path, value


def _stub(blobs: BlobStore, value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        data, media = value.encode("utf-8"), TEXT_TYPE
    else:
        data, media = canonical_bytes(value), JSON_TYPE
    return {"blob": blobs.put(data), "bytes": len(data), "type": media}


def _set(work: Any, path: Path, value: Any) -> None:
    target = work
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value


def offload(
    catalogue: Catalogue,
    blobs: BlobStore,
    name: str,
    payload: Mapping[str, Any],
    *,
    cap: int = PAYLOAD_CAP_BYTES,
) -> Mapping[str, Any]:
    """``payload`` with its largest values moved to ``blobs`` until it fits ``cap``.

    Returns ``payload`` itself when it already fits, and otherwise a plain (unfrozen)
    copy, which the catalogue checks on append like any payload. Checks the whole payload
    against the schema of ``name`` first (``payload_invalid``), and refuses a payload
    nothing more can be moved out of (``payload_too_large``).
    """
    if _size(payload) <= cap:
        return payload
    catalogue.validate_payload(name, payload, cap=None)
    keep = catalogue.dedup_fields(name)
    work = thaw(payload)
    while (size := _size(work)) > cap:
        picked = _pick(work, keep)
        if picked is None:
            raise PayloadTooLarge(name, size, cap)
        path, value = picked
        _set(work, path, _stub(blobs, value))
    moved: Mapping[str, Any] = work
    return moved


def offload_events(
    catalogue: Catalogue,
    blobs: BlobStore,
    events: Sequence[NewEvent],
    *,
    cap: int = PAYLOAD_CAP_BYTES,
) -> tuple[NewEvent, ...]:
    """Each event with its payload offloaded (:func:`offload`); a small event is unchanged."""
    out: list[NewEvent] = []
    for e in events:
        payload = offload(catalogue, blobs, e.type, e.payload, cap=cap)
        out.append(e if payload is e.payload else replace(e, payload=payload))
    return tuple(out)


def _inflated(value: Any, blobs: BlobStore | None) -> Any:
    if is_blob_stub(value):
        if blobs is None:
            raise BlobError("blob_store_required", f"a payload holds {value['blob']}")
        data = blobs.get(value["blob"])
        if value["type"] == TEXT_TYPE:
            return data.decode("utf-8")
        return _inflated(json.loads(data), blobs)
    if isinstance(value, Mapping):
        return {k: _inflated(v, blobs) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_inflated(v, blobs) for v in value]
    return value


def inflate(payload: Mapping[str, Any], blobs: BlobStore | None) -> Mapping[str, Any]:
    """``payload`` with every stub replaced by the value it stands for, frozen.

    A payload with no stub is returned as it is, and needs no blob store. A stub with no
    blob store is ``blob_store_required``; a missing or edited blob raises the store's
    ``blob_not_found`` or ``blob_corrupt``.
    """
    if not _has_stub(payload):
        return payload
    whole: Mapping[str, Any] = freeze(_inflated(payload, blobs))
    return whole


def _has_stub(value: Any) -> bool:
    if is_blob_stub(value):
        return True
    if isinstance(value, Mapping):
        return any(_has_stub(v) for v in value.values())
    if isinstance(value, list | tuple):
        return any(_has_stub(v) for v in value)
    return False


def blob_problems(events: Sequence[Event], blobs: BlobStore) -> tuple[tuple[int, str, str], ...]:
    """Every stub whose blob is missing or edited: ``(seq, blob ref, code)``, in seq order.

    The blob half of ADR-P01 H6: the chain covers each stub's blob hash, and this reads
    each blob back through the store, which re-hashes it.
    """
    problems: list[tuple[int, str, str]] = []
    for e in events:
        for ref in _stub_refs(e.payload):
            try:
                blobs.get(ref)
            except BlobError as err:
                problems.append((e.seq, ref, err.code))
    return tuple(problems)


def _stub_refs(value: Any) -> list[str]:
    if is_blob_stub(value):
        return [value["blob"]]
    if isinstance(value, Mapping):
        return [r for k in sorted(value) for r in _stub_refs(value[k])]
    if isinstance(value, list | tuple):
        return [r for v in value for r in _stub_refs(v)]
    return []
