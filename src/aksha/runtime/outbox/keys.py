"""Idempotency keys from what an effect means (ADR-P03 D2). Pure.

The key is ``sha256:<hex>`` over the canonical JSON of the tool name, the owner and the
values of the tool's declared ``key_fields``, each read by JSON pointer from the
arguments. Nothing about where in a run the effect happened (a step ID, a run ID, a
clock) enters it, so a rebuild run that regenerates an equal request reaches the same key
and the system of record returns the first receipt. The owner is in it, so two owners
building the same request get two ledger rows.

``resolve`` pins each value to what it means exactly: the artifact store (P04) turns a
reference into its exact version, and a recipient into its record version. Without it,
values are used as given.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from aksha.runtime.artifacts.types import ArtifactRef
from aksha.runtime.canonical import sha256_hex
from aksha.runtime.outbox.errors import KeyFieldMissing
from aksha.runtime.outbox.types import EffectSpec
from aksha.runtime.policy.rules import argument_at

Resolve = Callable[[str, Any], Any]
"""``(pointer, value) -> value``: the exact version a reference stands for."""


def key_values(
    args: Mapping[str, Any], spec: EffectSpec, resolve: Resolve | None = None
) -> dict[str, Any]:
    """The value at each key field, resolved; ``KeyFieldMissing`` for an absent one."""
    out: dict[str, Any] = {}
    for pointer in spec.key_fields:
        found, value = argument_at(args, pointer)
        if not found:
            raise KeyFieldMissing(message=f"key field {pointer} is not in the arguments")
        out[pointer] = value if resolve is None else resolve(pointer, value)
    return out


def idempotency_key(
    tool: str,
    owner: str,
    args: Mapping[str, Any],
    spec: EffectSpec,
    *,
    resolve: Resolve | None = None,
) -> str:
    """``sha256:<hex>`` over the tool, the owner and the resolved key-field values."""
    fields = key_values(args, spec, resolve)
    return "sha256:" + sha256_hex({"tool": tool, "owner": owner, "fields": fields})


def request_hash(request: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` of the whole request, so a change outside the key fields is seen."""
    return "sha256:" + sha256_hex(request)


def record_values(args: Mapping[str, Any], spec: EffectSpec) -> dict[str, Any]:
    """The value at each record field (ADR-P29 D1), as given; ``record_field_missing``.

    Record identity is never resolved to an exact version: a form kind and a candidate
    name one outside record across every version of the form written to it.
    """
    out: dict[str, Any] = {}
    for pointer in spec.record_fields:
        found, value = argument_at(args, pointer)
        if not found:
            raise KeyFieldMissing(
                "record_field_missing", f"record field {pointer} is not in the arguments"
            )
        out[pointer] = value
    return out


def record_key(tool: str, owner: str, args: Mapping[str, Any], spec: EffectSpec) -> str:
    """``sha256:<hex>`` over the tool, the owner and the record-field values (ADR-P29)."""
    fields = record_values(args, spec)
    return "sha256:" + sha256_hex({"tool": tool, "owner": owner, "record": fields})


def artifact_sources(values: Mapping[str, Any]) -> tuple[str, ...]:
    """The artifact refs among ``values`` (each ``kind/key@vN`` string, at any depth), in order.

    These are the versions a request was written from (ADR-P29, amendment B2): the key
    fields name what the effect means, so an artifact version among them is its source.
    """
    found: list[str] = []
    for value in values.values():
        _refs(value, found)
    return tuple(dict.fromkeys(found))


def _refs(value: Any, found: list[str]) -> None:
    if isinstance(value, str):
        try:
            found.append(str(ArtifactRef.parse(value)))
        except ValueError:
            return
    elif isinstance(value, Mapping):
        for v in value.values():
            _refs(v, found)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _refs(v, found)
