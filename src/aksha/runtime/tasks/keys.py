"""Key sources read from the event store (ADR-P15 D3): which keys a fan-out has now.

Two origins:

1. ``artifact_keys(kind, path)``: the keys listed at ``path`` (a dotted field) of the
   current version of an unkeyed ``kind`` artifact. A list item is a string, or an object
   whose ``key`` or ``id`` field is one (a plan's ``steps``). None while no version exists:
   the fan-out has no instances yet, rather than zero.
2. ``source_keys(collection)``: the keys the latest ``keys.listed`` fact recorded for the
   collection, then every later ``source.changed`` record of it applied: a record that
   went (``None``) takes its key out, a record with a version puts it back (S3). None
   while the collection was never listed.

Keys come back sorted as strings, so two fake-model runs give the same order. A key the
instance-ID grammar cannot carry (empty, or holding ``/``, ``[`` or ``]``) is refused when
it is listed (``key_invalid``) and left out when an artifact lists it (``invalid_keys``
names it, so the save that wrote it can be found). :func:`key_entry` gives the entry one
key stands for, which a keyed instance's context shows (ADR-P08 D3), and
:func:`key_entry_pointer` where it sits, which that instance records as its read.
``keys_of`` does not apply a key source's ``unless`` filter: that needs a Task view, and
``status.instance_keys`` applies it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from aksha.runtime.artifacts.versions import current
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.errors import KeyInvalid
from aksha.runtime.tasks.ports import KeyLister
from aksha.runtime.tasks.types import KeySource

KEYS_LISTED = "keys.listed"
SOURCE_CHANGED = "source.changed"


def is_valid_key(key: str) -> bool:
    """True for a key an instance ID can carry: not empty, no ``/``, ``[`` or ``]``."""
    return bool(key) and not any(c in key for c in "/[]")


def _field(content: Mapping[str, Any], path: str) -> object:
    value: object = content
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _item_key(item: object) -> str | None:
    if isinstance(item, Mapping):
        item = item.get("key", item.get("id"))
    if isinstance(item, bool) or not isinstance(item, str | int):
        return None
    return str(item)


def _artifact_items(view: LogView, kind: str, path: str) -> tuple[str, ...] | None:
    version = current(view, kind, None)
    if version is None:
        return None
    listed = _field(version.content, path)
    if not isinstance(listed, Sequence) or isinstance(listed, str):
        return ()
    return tuple(k for k in (_item_key(i) for i in listed) if k is not None)


def key_entry(
    view: LogView, source: KeySource, key: str, *, kind: str | None = None
) -> object | None:
    """What ``source`` lists for ``key``: its list item, or ``collection/key`` for a record.

    For an artifact source it is the item at ``source.path`` of the current ``kind``
    version whose key (a string item, or its ``key`` or ``id`` field) is ``key``; None when
    no version or no item has it. For a source collection it is the record's ref. The
    context of a keyed instance shows it, so the model knows which entry it works on.
    """
    if source.origin == "source":
        return f"{source.name}/{key}"
    version = current(view, kind or source.name, None)
    if version is None:
        return None
    listed = _field(version.content, source.path or "")
    if not isinstance(listed, Sequence) or isinstance(listed, str):
        return None
    return next((i for i in listed if _item_key(i) == key), None)


def key_entry_pointer(
    view: LogView, source: KeySource, key: str, *, kind: str | None = None
) -> str | None:
    """The JSON pointer of ``key``'s list item in the current ``kind`` version, or None.

    It is ``/`` plus ``source.path`` with dots as slashes, then the item's index, such as
    ``/steps/1``. A keyed instance records its read of the key source at this pointer, so a
    later version that changes only another key's entry does not reopen it. None for a
    source collection, and when no version or no item has the key.
    """
    if source.origin == "source":
        return None
    version = current(view, kind or source.name, None)
    if version is None:
        return None
    listed = _field(version.content, source.path or "")
    if not isinstance(listed, Sequence) or isinstance(listed, str):
        return None
    index = next((n for n, i in enumerate(listed) if _item_key(i) == key), None)
    if index is None:
        return None
    base = "/" + (source.path or "").replace(".", "/") if source.path else ""
    return f"{base}/{index}"


def source_keys_listed(view: LogView, collection: str) -> tuple[str, ...] | None:
    """The collection's keys as the log last recorded them (module doc, origin 2)."""
    keys: set[str] | None = None
    prefix = f"{collection}/"
    for e in view.events:
        if e.type == KEYS_LISTED and e.payload.get("collection") == collection:
            keys = {str(k) for k in e.payload.get("keys", ())}
        elif e.type == SOURCE_CHANGED and keys is not None:
            for record, version in e.payload.get("records", {}).items():
                if str(record).startswith(prefix):
                    key = str(record)[len(prefix) :]
                    if version is None:
                        keys.discard(key)
                    else:
                        keys.add(key)
    return None if keys is None else tuple(sorted(keys))


def keys_of(view: LogView, source: KeySource, *, kind: str | None = None) -> tuple[str, ...] | None:
    """The sorted, valid keys ``source`` has in ``view``, or None before it has any record.

    ``kind`` is the artifact kind's full name when ``source.name`` is local (the compiled
    tree resolves it); it defaults to ``source.name``.
    """
    if source.origin == "source":
        found = source_keys_listed(view, source.name)
    else:
        found = _artifact_items(view, kind or source.name, source.path or "")
    if found is None:
        return None
    return tuple(sorted({k for k in found if is_valid_key(k)}))


def invalid_keys(view: LogView, source: KeySource, *, kind: str | None = None) -> tuple[str, ...]:
    """The keys an artifact lists that an instance ID cannot carry, sorted."""
    if source.origin == "source":
        return ()
    found = _artifact_items(view, kind or source.name, source.path or "") or ()
    return tuple(sorted({k for k in found if not is_valid_key(k)}))


def keys_listed_fact(collection: str, keys: Iterable[str]) -> NewEvent:
    """The ``keys.listed`` fact for ``collection``; ``key_invalid`` for a key it cannot carry."""
    listed = sorted(set(keys))
    bad = [k for k in listed if not is_valid_key(k)]
    if bad:
        raise KeyInvalid(message=f"{collection}: keys {bad!r} cannot be part of an instance ID")
    return NewEvent(KEYS_LISTED, {"collection": collection, "keys": listed})


def list_source_keys(
    view: LogView, lister: KeyLister, collections: Iterable[str]
) -> tuple[NewEvent, ...]:
    """A ``keys.listed`` fact for each collection whose keys differ from the recorded ones.

    Reads the lister, never appends; the caller commits the facts in its stop decision
    (U3) or source change (U8). Nothing is returned when no key moved.
    """
    out: list[NewEvent] = []
    for collection in sorted(set(collections)):
        now = tuple(sorted(set(lister.keys(view.owner, collection))))
        if now != source_keys_listed(view, collection):
            out.append(keys_listed_fact(collection, now))
    return tuple(out)
