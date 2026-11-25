"""Canonical forms shared by the whole runtime: canonical JSON, hashing and UTC timestamps.

One definition each of canonical JSON (sorted keys, compact separators), SHA-256 hashing
over it, deep freezing of payloads, and the UTC timestamp format written into every
event. Event payload validation, artifact hashes, idempotency keys and approval
signatures all build on these, so they agree byte for byte.

:func:`canonical_bytes` is the stricter form the event store's hash chain uses (ADR-P01
Amendment H, H3): the subset of RFC 8785 (the JSON Canonicalization Scheme) with no floats
and integers within plus or minus 2**53, where JCS and this encoder agree exactly. Every
event payload is checked against it on append, so both backends hash the same bytes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from aksha.runtime.errors import AkshaError


def _default(value: object) -> object:
    """Let ``json`` serialise frozen mappings; refuse every other unknown type."""
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"not JSON-serialisable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialise ``value`` with sorted keys, no spaces and UTF-8 kept as is.

    Accepts frozen values from :func:`freeze`. NaN and infinity are refused (``ValueError``),
    because they have no JSON form and would hash differently across backends.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_default,
    )


NUL = "\x00"
"""The one character PostgreSQL's ``text`` and ``jsonb`` cannot hold; refused everywhere."""


def nul_path(value: Any, path: str = "$") -> str | None:
    """The JSON path of the first string (key or value) in ``value`` holding NUL, else None.

    Every backend must accept the same facts, and PostgreSQL refuses NUL in text, so a
    payload or an artifact's content with NUL is refused before any backend sees it.
    """
    if isinstance(value, str):
        return path if NUL in value else None
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and NUL in key:
                return f"{path}.{key!r}"
            found = nul_path(item, f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            found = nul_path(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def sha256_hex(value: Any) -> str:
    """The hex SHA-256 of ``value``'s canonical JSON, encoded as UTF-8."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def freeze(value: Any) -> Any:
    """Deep-freeze a JSON value: mappings become read-only proxies and lists become tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze(v) for v in value)
    return value


def thaw(value: Any) -> Any:
    """The inverse of :func:`freeze`: plain dicts and lists, for code that must mutate a copy."""
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [thaw(v) for v in value]
    return value


def iso_utc(moment: datetime) -> str:
    """Format an aware ``moment`` in UTC with microseconds: ``2026-11-27T09:00:00.000000Z``.

    A naive datetime is refused (``ValueError``): every recorded time comes from the
    injected clock, which is always aware.
    """
    if moment.tzinfo is None:
        raise ValueError("a recorded time must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


MAX_EXACT_INTEGER = 2**53
"""The largest magnitude of an integer the chain's encoding accepts (H3)."""


class NotCanonical(AkshaError, ValueError):
    """A value outside the chain's encoding; ``path`` names it, e.g. ``$.items[1]``."""

    default_code = "not_canonical"

    def __init__(self, path: str, reason: str) -> None:
        """Name the JSON path of the refused value and why it is refused."""
        self.path = path
        self.reason = reason
        super().__init__(message=f"{path}: {reason}")


def _utf16_key(key: str) -> bytes:
    """Sort key for RFC 8785: UTF-16 code units, which differs from code points past U+FFFF."""
    return key.encode("utf-16-be", errors="surrogatepass")


def _encode(value: Any, path: str, out: list[str]) -> None:
    if value is None or isinstance(value, bool):
        out.append({None: "null", True: "true", False: "false"}[value])
    elif isinstance(value, int):
        if abs(value) > MAX_EXACT_INTEGER:
            raise NotCanonical(path, f"integer outside plus or minus 2**53: {value}")
        out.append(str(value))
    elif isinstance(value, float):
        raise NotCanonical(path, "floats are refused; use an integer in fixed units or a string")
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise NotCanonical(path, "a string holds a lone surrogate") from None
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise NotCanonical(path, "object keys must be strings")
        out.append("{")
        for i, key in enumerate(sorted(value, key=_utf16_key)):
            if i:
                out.append(",")
            _encode(key, path, out)
            out.append(":")
            _encode(value[key], f"{path}.{key}", out)
        out.append("}")
    elif isinstance(value, list | tuple):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _encode(item, f"{path}[{i}]", out)
        out.append("]")
    else:
        raise NotCanonical(path, f"not a JSON value: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    """Encode ``value`` in the RFC 8785 subset of ADR-P01 H3, as UTF-8 bytes.

    Object keys are sorted by UTF-16 code units, there is no insignificant whitespace,
    strings use JCS's minimal escaping, and ``true``, ``false`` and ``null`` are literal.
    Accepts frozen values from :func:`freeze`. Raises :class:`NotCanonical`, naming the
    path, for a float (NaN included), an integer beyond plus or minus 2**53, a non-string
    key, a lone surrogate or any non-JSON type.
    """
    out: list[str] = []
    _encode(value, "$", out)
    return "".join(out).encode("utf-8")


def sha256_ref(data: bytes) -> str:
    """``"sha256:"`` plus the 64 lowercase hex characters of ``data``'s SHA-256."""
    return "sha256:" + hashlib.sha256(data).hexdigest()
