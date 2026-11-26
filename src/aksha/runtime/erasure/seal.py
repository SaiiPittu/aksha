"""Sealing personal values out of a payload, and resolving them back (ADR-P30 E1 to E3). Pure.

:func:`seal` replaces each :class:`~aksha.runtime.erasure.types.Personal` in a payload
with a placeholder ``{"$erasable": ref, "commit": ..., "redacted": ...}`` and returns the
sealed values beside it. The event store hashes and stores the sealed payload, so the
chain covers the commitment and never the value, and erasing the value later changes no
hashed byte. :func:`resolve` puts each value back, or its stand-in once it is erased.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from aksha.runtime.canonical import NUL, NotCanonical, canonical_bytes, sha256_ref
from aksha.runtime.erasure.ports import Entropy
from aksha.runtime.erasure.types import (
    SUBJECT_MAX,
    ErasureError,
    Personal,
    Placeholder,
    SealedValue,
)

PLACEHOLDER_KEY: Final = "$erasable"
"""The key that marks a placeholder. Reserved: a caller's payload may not use it."""

_PLACEHOLDER_KEYS: Final = frozenset({PLACEHOLDER_KEY, "commit", "redacted"})


def commitment(salt: str, subject: str, value: Any) -> str:
    """``sha256:`` over the canonical bytes of ``{"salt", "subject", "value"}``.

    The subject is bound in, so a row moved to another subject no longer verifies and
    ``erase`` cannot be made to miss it.
    """
    return sha256_ref(canonical_bytes({"salt": salt, "subject": subject, "value": value}))


def is_placeholder(value: Any) -> bool:
    """Whether ``value`` is a stored placeholder."""
    return isinstance(value, Mapping) and set(value) == _PLACEHOLDER_KEYS


def _check_json(value: Any, what: str) -> None:
    if _has_personal(value):
        raise ErasureError("erasure_nested", f"{what} holds another personal value")
    try:
        canonical_bytes(value)
    except (NotCanonical, TypeError, ValueError) as e:
        raise ErasureError("erasure_value_invalid", f"{what} is not canonical JSON: {e}") from None


def _has_personal(value: Any) -> bool:
    if isinstance(value, Personal):
        return True
    if isinstance(value, Mapping):
        return any(_has_personal(v) for v in value.values())
    if isinstance(value, list | tuple):
        return any(_has_personal(v) for v in value)
    return False


def has_personal(value: Any) -> bool:
    """Whether ``value`` holds a :class:`Personal` anywhere."""
    return _has_personal(value)


def _sealed(marker: Personal, entropy: Entropy | None) -> SealedValue:
    subject = marker.subject
    if not isinstance(subject, str) or not subject or NUL in subject or len(subject) > SUBJECT_MAX:
        raise ErasureError(
            "erasure_subject_invalid",
            f"a subject is a non-empty string of at most {SUBJECT_MAX} characters, without NUL",
        )
    _check_json(marker.value, f"the value for {subject!r}")
    _check_json(marker.redacted, f"the stand-in for {subject!r}")
    if entropy is None:
        raise ErasureError(
            "erasure_no_entropy", "sealing a personal value needs the store's entropy source"
        )
    salt = entropy(32)
    return SealedValue(
        ref=f"er_{entropy(16)}",
        subject=subject,
        salt=salt,
        value=marker.value,
        commit=commitment(salt, marker.subject, marker.value),
    )


def seal(
    payload: Mapping[str, Any], *, entropy: Entropy | None = None
) -> tuple[dict[str, Any], tuple[SealedValue, ...]]:
    """The payload with every :class:`Personal` replaced by a placeholder, and the values.

    Salts and refs come from ``entropy`` (the runtime reads no ambient randomness, ADR-P02
    D3); the storage adapters pass ``secrets.token_hex``. A payload with no personal value
    needs none, and one with a value but no entropy is refused (``erasure_no_entropy``).

    Refuses a caller key named :data:`PLACEHOLDER_KEY` anywhere (``erasure_reserved_key``),
    a personal value inside another (``erasure_nested``), a bad subject and a value or
    stand-in outside the chain's encoding.
    """
    found: list[SealedValue] = []

    def walk(value: Any) -> Any:
        if isinstance(value, Personal):
            sealed = _sealed(value, entropy)
            found.append(sealed)
            return {PLACEHOLDER_KEY: sealed.ref, "commit": sealed.commit,
                    "redacted": value.redacted}  # fmt: skip
        if isinstance(value, Mapping):
            if PLACEHOLDER_KEY in value:
                raise ErasureError(
                    "erasure_reserved_key", f"{PLACEHOLDER_KEY!r} is reserved for placeholders"
                )
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [walk(v) for v in value]
        return value

    out = walk(payload)
    assert isinstance(out, dict)
    return out, tuple(found)


def _swap(value: Any, pick: Any) -> Any:
    if isinstance(value, Personal):
        return pick(value)
    if isinstance(value, Mapping):
        return {k: _swap(v, pick) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_swap(v, pick) for v in value]
    return value


def unsealed(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The payload with each :class:`Personal` replaced by its value (what is validated)."""
    out: dict[str, Any] = _swap(payload, lambda p: p.value)
    return out


def redacted(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The payload with each :class:`Personal` replaced by its stand-in (replay after erasure)."""
    out: dict[str, Any] = _swap(payload, lambda p: p.redacted)
    return out


def placeholders(payload: Any) -> tuple[Placeholder, ...]:
    """Every placeholder in a stored payload, in payload order (keys in stored order)."""
    found: list[Placeholder] = []

    def walk(value: Any) -> None:
        if is_placeholder(value):
            found.append(
                Placeholder(str(value[PLACEHOLDER_KEY]), str(value["commit"]), value["redacted"])
            )
        elif isinstance(value, Mapping):
            for v in value.values():
                walk(v)
        elif isinstance(value, list | tuple):
            for v in value:
                walk(v)

    walk(payload)
    return tuple(found)


def resolve(payload: Any, values: Mapping[str, Any]) -> Any:
    """The stored payload with each placeholder replaced by its value, or its stand-in.

    ``values`` maps a ref to its value for every value still held; a ref missing from it
    has been erased, and the reader sees the placeholder's ``redacted`` stand-in.
    """
    if is_placeholder(payload):
        ref = str(payload[PLACEHOLDER_KEY])
        return values[ref] if ref in values else payload["redacted"]
    if isinstance(payload, Mapping):
        return {k: resolve(v, values) for k, v in payload.items()}
    if isinstance(payload, list | tuple):
        return [resolve(v, values) for v in payload]
    return payload
