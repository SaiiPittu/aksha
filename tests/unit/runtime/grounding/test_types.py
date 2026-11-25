"""Grounding values: cite policies, cite forms and explicit unknowns (ADR-P13 D4, D5)."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from aksha.runtime.grounding.types import (
    UNKNOWN_SCHEMA,
    ArtifactCite,
    CitePolicy,
    SpanCite,
    StatementCite,
    Unknown,
    form_name,
    maybe_unknown,
    parse_cite,
)


def test_cite_policy_refuses_a_bad_pattern_or_form_list() -> None:
    for field in ("cells", ""):
        with pytest.raises(ValueError):
            CitePolicy(field)
    with pytest.raises(ValueError):
        CitePolicy("/x", forms=frozenset())
    with pytest.raises(ValueError):
        CitePolicy("/x", forms=frozenset({"guess"}))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CitePolicy("/x", forms=frozenset({"statement"}), value_in_quote=True)
    assert CitePolicy("/cells/*/a~1b").parts() == ["cells", "*", "a/b"]


def test_parse_cite_reads_the_three_forms_and_nothing_else() -> None:
    assert parse_cite({"span": "EX-0001", "quote": "q"}) == SpanCite("EX-0001", "q")
    assert parse_cite({"statement": "EX-0002"}) == StatementCite("EX-0002")
    assert parse_cite({"artifact": "a.b@v1"}) == ArtifactCite("a.b@v1", "")
    assert parse_cite({"artifact": "a.b@v1", "path": "/x"}) == ArtifactCite("a.b@v1", "/x")
    for bad in (
        "EX-0001",
        {"span": "EX-0001"},
        {"span": "", "quote": "q"},
        {"span": "EX-0001", "quote": "q", "extra": 1},
        {"statement": 3},
        {"artifact": "a.b@v1", "path": 1},
    ):
        assert parse_cite(bad) is None
    assert [form_name(c) for c in (SpanCite("a", "b"), StatementCite("a"), ArtifactCite("a"))] == [
        "span",
        "statement",
        "artifact",
    ]


def test_unknown_round_trips_and_refuses_a_bad_shape() -> None:
    u = Unknown("What is the price?", "unverified", failed_step="R-0001/g/t01/c01")
    assert Unknown.from_json(u.as_json()) == u
    assert Unknown.from_json(Unknown("q?", "not_found", ("record:a/b@1",)).as_json()).checked == (
        "record:a/b@1",
    )
    for bad in (
        {"unknown": {"question": "", "reason": "not_found"}},
        {"unknown": {"question": "q", "reason": "zero"}},
        {"unknown": {"question": "q", "reason": "not_found", "guess": 0}},
        {"unknown": {"question": "q", "reason": "not_found", "checked": "a"}},
        {"unknown": {"question": "q", "reason": "unverified", "failed_step": ""}},
        {"unknown": "q", "value": 0},
    ):
        with pytest.raises(ValueError):
            Unknown.from_json(bad)


def test_maybe_unknown_accepts_the_value_or_an_unknown_only() -> None:
    validator = Draft202012Validator(maybe_unknown({"type": "integer"}))
    assert validator.is_valid(279)
    assert validator.is_valid(Unknown("q?", "not_checked").as_json())
    assert not validator.is_valid({"unknown": {"question": "q?"}})
    assert not validator.is_valid("279")
    Draft202012Validator.check_schema(dict(UNKNOWN_SCHEMA))
