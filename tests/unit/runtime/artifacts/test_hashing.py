"""The three hashes (ADR-P04 D5) and the entry hash of a path-scoped read (S3)."""

from __future__ import annotations

import pytest

from aksha.runtime.artifacts.hashing import (
    canonical_form,
    canonical_hash,
    content_hash,
    entry_hash,
    reads_hash,
    resolve,
)
from aksha.runtime.artifacts.types import ArtifactKind, InputRef

KIND = ArtifactKind("line", {"type": "object"})


def _cite(record: str, version: str, quote: str) -> dict[str, object]:
    source = {"kind": "record", "id": record, "version": version}
    return {"source": source, "quote": quote, "pointer": "/body"}


def test_canonical_form_reduces_refs_and_cites() -> None:
    """The same quote from a moved record and a moved ref version hash the same."""
    a = {"see": "notes.line/n1@v1", "cite": _cite("pages/P-0001", "1", "279 a month")}
    b = {"see": "notes.line/n1@v4", "cite": _cite("pages/P-0001", "7", "279 a month")}
    assert canonical_hash(KIND, a) == canonical_hash(KIND, b)
    assert content_hash(a) != content_hash(b)
    c = {"see": "notes.line/n2@v1", "cite": _cite("pages/P-0001", "1", "279 a month")}
    d = {"see": "notes.line/n1@v1", "cite": _cite("pages/P-0001", "1", "300 a month")}
    assert canonical_hash(KIND, c) != canonical_hash(KIND, a) != canonical_hash(KIND, d)


def test_canonical_form_reduces_a_span_cite_to_its_quote() -> None:
    """ADR-P13: a re-minted span with the same words is the same claim."""
    a = {"price": {"value": 279, "cite": {"span": "EX-0001", "quote": "279 EUR per sensor"}}}
    b = {"price": {"value": 279, "cite": {"span": "EX-0042", "quote": "279 EUR per sensor"}}}
    c = {"price": {"value": 279, "cite": {"span": "EX-0001", "quote": "300 EUR per sensor"}}}
    assert canonical_hash(KIND, a) == canonical_hash(KIND, b) != canonical_hash(KIND, c)
    assert content_hash(a) != content_hash(b)


def test_canonical_form_drops_excluded_pointers_and_sorts_keyed_lists() -> None:
    kind = ArtifactKind(
        "matrix", {"type": "object"}, canon_exclude=("/fetched_at", "/rows/0/note"),
        list_keys={"/rows": "id"},
    )  # fmt: skip
    one = {"fetched_at": "mon", "rows": [{"id": "b", "note": "x"}, {"id": "a"}]}
    two = {"fetched_at": "tue", "rows": [{"id": "a"}, {"id": "b"}]}
    assert canonical_form(kind, one) == {"rows": [{"id": "a"}, {"id": "b"}]}
    assert canonical_hash(kind, one) == canonical_hash(kind, two)


def test_the_canonicalize_hook_runs_first_and_changes_nothing_it_is_given() -> None:
    seen: list[object] = []

    def strip(content: object) -> dict[str, object]:
        seen.append(content)
        return {"code": str(content["code"]).strip()}  # type: ignore[index]

    kind = ArtifactKind("source", {"type": "object"}, canonicalize=strip)
    original = {"code": "  x = 1  "}
    assert canonical_hash(kind, original) == canonical_hash(kind, {"code": "x = 1"})
    assert original == {"code": "  x = 1  "} and seen


def test_reads_hash_ignores_order_and_repeats() -> None:
    a, b = InputRef("record", "pages/P-1", "1"), InputRef("query", "Q-1", "sha256:" + "0" * 64)
    assert reads_hash([a, b]) == reads_hash([b, a, a]) != reads_hash([a])


def test_entry_hash_and_pointers() -> None:
    content = {"files": {"a/b.py": "x"}, "rows": [{"id": 1}]}
    assert entry_hash(content, "/files/a~1b.py") == entry_hash({"v": "x"}, "/v")
    assert resolve(content, "/rows/0/id") == (True, 1)
    assert resolve(content, "/rows/5") == (False, None)
    with pytest.raises(KeyError):
        entry_hash(content, "/nothing")
    with pytest.raises(ValueError):
        resolve(content, "rows")
