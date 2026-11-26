"""The one verifier (ADR-P13 D2, D4, D5, S2, S3), on values built by hand."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from aksha.runtime.artifacts.types import ArtifactRef
from aksha.runtime.grounding.types import (
    ArtifactCite,
    CitationSpan,
    CiteError,
    CiteForm,
    CitePolicy,
    SpanCite,
    SpanIndex,
    StatementCite,
    Unknown,
    Verified,
)
from aksha.runtime.grounding.verify import Evidence, normalise, verify_citation, verify_content

RUN = "R-0001"
ME = (RUN, "compare[C2]")
PRICE = "Pricing: 279 EUR per sensor, billed yearly."
LINE = "def login(user):  # line 42"


def _span(n: int, text: str, **kw: Any) -> CitationSpan:
    fields: dict[str, Any] = {
        "span_id": f"EX-{n:04d}", "tenant": "o1", "source": "fixtures",
        "record_id": f"pages/P-{n:04d}", "version": "1", "start": 0, "end": len(text),
        "text": text, "tool": "fetch_page", "shown_to": frozenset({ME}),
    }  # fmt: skip
    fields.update(kw)
    return CitationSpan(**fields)


SPANS = SpanIndex(
    "o1",
    {
        s.span_id: s
        for s in (
            _span(1, PRICE),
            _span(2, PRICE, shown_to=frozenset({(RUN, "compare[C1]")})),
            _span(3, "Pricing 300 EUR", record_id="pages/P-0003", version="1"),
            _span(4, "We file in March.", source="statement", record_id="I-0001", tool="ask"),
            _span(
                5,
                "We file in May.",
                source="statement",
                record_id="I-0001",
                version="C-2",
                tool="ask",
                supersedes="EX-0004",
            ),
            _span(6, LINE, source="tree", record_id="tree@T7:src/auth.py", version="h7"),
            _span(7, "short"),
        )
    },
)
CONTENT: Mapping[str, Mapping[str, Any]] = {"notes.matrix@v1": {"rows": [{"price": 279}]}}


def evidence(**kw: Any) -> Evidence:
    fields: dict[str, Any] = {
        "spans": SPANS,
        "run": RUN,
        "instance": "compare[C2]",
        "records": {"pages/P-0003": "2"},
        "reads": frozenset({"artifact:notes.matrix@v1"}),
        "artifact": lambda ref: CONTENT.get(str(ref)),
        "failed_steps": frozenset({"R-0001/gather/t02/c01"}),
    }
    fields.update(kw)
    return Evidence(**fields)


ALL = CitePolicy("/x", forms=frozenset({"span", "statement", "artifact"}))

SCENARIOS: list[tuple[str | None, CiteForm, CitePolicy]] = [
    (None, SpanCite("EX-0001", "279 EUR per sensor"), ALL),
    ("unknown_span", SpanCite("EX-0099", "279 EUR per sensor"), ALL),
    ("span_not_seen", SpanCite("EX-0002", "279 EUR per sensor"), ALL),
    ("stale_source", SpanCite("EX-0003", "Pricing 300 EUR"), ALL),
    ("quote_not_verbatim", SpanCite("EX-0001", "279 euro per sensor"), ALL),
    ("quote_too_short", SpanCite("EX-0001", "279 EUR"), ALL),
    ("form_not_allowed", StatementCite("EX-0005"), CitePolicy("/x")),
    ("superseded_statement", StatementCite("EX-0004"), ALL),
    ("stale_artifact_cite", ArtifactCite("notes.matrix@v1", "/rows/9"), ALL),
]


@pytest.mark.parametrize(("code", "cite", "policy"), SCENARIOS)
def test_citation_checker_all_scenarios(
    code: str | None, cite: CiteForm, policy: CitePolicy
) -> None:
    """Each code fires on its fixture and only there."""
    result = verify_citation(cite, evidence(), policy, pointer="/x/cite")
    if code is None:
        assert isinstance(result, Verified)
    else:
        assert isinstance(result, CiteError) and result.code == code
        assert result.pointer == "/x/cite"
    for other, other_cite, other_policy in SCENARIOS:
        if other != code:
            again = verify_citation(other_cite, evidence(), other_policy)
            assert not (isinstance(again, CiteError) and again.code == code)


def test_a_verified_span_cite_records_the_quote_and_its_place() -> None:
    result = verify_citation(SpanCite("EX-0001", "279  EUR\nper sensor"), evidence())
    assert result == Verified("", "span", "EX-0001", quote="279 EUR per sensor", occurrence=9)


def test_whitespace_only_normalisation() -> None:
    """Case, curly quotes and dashes are not normalised; whitespace runs are."""
    assert normalise("  a \t b\n\nc ") == "a b c"
    span = _span(8, "The \u201cPro\u201d plan \u2013 279 EUR per sensor")
    ev = evidence(spans=SpanIndex("o1", {span.span_id: span}))
    for quote in ('the "Pro" plan', 'The "Pro" plan - 279', "THE \u201cPRO\u201d PLAN"):
        result = verify_citation(SpanCite("EX-0008", quote), ev)
        assert isinstance(result, CiteError) and result.code == "quote_not_verbatim"
    ok = verify_citation(SpanCite("EX-0008", "The \u201cPro\u201d   plan \u2013 279"), ev)
    assert isinstance(ok, Verified)


def test_quote_not_verbatim_hands_the_model_the_span_text() -> None:
    result = verify_citation(SpanCite("EX-0001", "279 euro"), evidence())
    assert isinstance(result, CiteError) and PRICE in result.message


def test_a_short_span_may_be_quoted_whole() -> None:
    assert isinstance(verify_citation(SpanCite("EX-0007", "short"), evidence()), Verified)


def test_span_not_seen_by_instance() -> None:
    """A span shown only to compare[C1] cannot be cited by compare[C2], nor in another run."""
    seen_by_c1 = SpanCite("EX-0002", "279 EUR per sensor")
    c1 = verify_citation(seen_by_c1, evidence(instance="compare[C1]"))
    assert isinstance(c1, Verified)
    c2 = verify_citation(seen_by_c1, evidence())
    assert isinstance(c2, CiteError) and c2.code == "span_not_seen"
    other_run = verify_citation(SpanCite("EX-0001", "279 EUR per sensor"), evidence(run="R-0002"))
    assert isinstance(other_run, CiteError) and other_run.code == "span_not_seen"


def test_stale_source_is_record_grain_and_names_the_current_version() -> None:
    """S2: a change to another record of the source leaves this span citable."""
    moved = verify_citation(SpanCite("EX-0003", "Pricing 300 EUR"), evidence())
    assert isinstance(moved, CiteError) and moved.code == "stale_source"
    assert "at version 2" in moved.message
    gone = verify_citation(
        SpanCite("EX-0003", "Pricing 300 EUR"), evidence(records={"pages/P-0003": None})
    )
    assert isinstance(gone, CiteError) and "gone" in gone.message
    untouched = verify_citation(SpanCite("EX-0001", "279 EUR per sensor"), evidence())
    assert isinstance(untouched, Verified)


def test_a_tree_span_is_cited_like_any_other() -> None:
    """D3: the span's source can be anything versioned; the verifier does not care."""
    result = verify_citation(SpanCite("EX-0006", "def login(user):"), evidence())
    assert isinstance(result, Verified)
    stale = verify_citation(
        SpanCite("EX-0006", "def login(user):"), evidence(records={"tree@T7:src/auth.py": "h8"})
    )
    assert isinstance(stale, CiteError) and stale.code == "stale_source"


def test_policy_narrows_span_sources_and_artifact_kinds() -> None:
    only_tree = CitePolicy("/x", span_sources=frozenset({"tree"}))
    wrong = verify_citation(SpanCite("EX-0001", "279 EUR per sensor"), evidence(), only_tree)
    assert isinstance(wrong, CiteError) and wrong.code == "form_not_allowed"
    only_notes = CitePolicy("/x", forms=frozenset({"artifact"}), artifact_kinds=frozenset({"p.x"}))
    kind = verify_citation(ArtifactCite("notes.matrix@v1", "/rows/0"), evidence(), only_notes)
    assert isinstance(kind, CiteError) and kind.code == "form_not_allowed"


def test_a_statement_span_is_no_span_cite_unless_statements_are_allowed() -> None:
    """A matrix cell may not cite a user statement by naming its span as a span."""
    result = verify_citation(SpanCite("EX-0005", "We file in May."), evidence(), CitePolicy("/x"))
    assert isinstance(result, CiteError) and result.code == "form_not_allowed"
    ok = verify_citation(StatementCite("EX-0005"), evidence(), ALL)
    assert isinstance(ok, Verified) and ok.form == "statement"


def test_artifact_cites() -> None:
    ok = verify_citation(ArtifactCite("notes.matrix@v1", "/rows/0"), evidence())
    assert ok == Verified("", "artifact", "notes.matrix@v1", path="/rows/0")
    unread = verify_citation(ArtifactCite("notes.matrix@v1"), evidence(reads=frozenset()))
    assert isinstance(unread, CiteError) and unread.code == "span_not_seen"
    bad = verify_citation(ArtifactCite("not a ref"), evidence())
    assert isinstance(bad, CiteError) and bad.code == "cite_invalid"
    assert ArtifactRef.parse(ok.target).version == 1


# --- content -------------------------------------------------------------------------------

CELLS = (
    CitePolicy("/cells/*/price", may_be_unknown=True, value_in_quote=True),
    CitePolicy("/cells/*/vendor_note", forms=frozenset({"span", "statement"})),
)


def _cell(value: Any) -> dict[str, Any]:
    return {"cells": [{"price": value}]}


def _codes(content: Mapping[str, Any], **kw: Any) -> list[str]:
    return [
        r.code for r in verify_content(content, CELLS, evidence(**kw)) if isinstance(r, CiteError)
    ]


def test_a_cited_field_passes_and_is_bound() -> None:
    cited = {"value": "279 EUR", "cite": {"span": "EX-0001", "quote": "279 EUR per sensor"}}
    (result,) = verify_content(_cell(cited), CELLS, evidence())
    assert isinstance(result, Verified) and result.pointer == "/cells/0/price/cite"


def test_missing_is_unknown_not_zero() -> None:
    """A price never found is an explicit unknown; 0, "" and null are ungrounded."""
    for bare in (0, "", None, {"value": 0}):
        assert _codes(_cell(bare)) == ["ungrounded_value"]
    unknown = Unknown("What does COMP-02 charge?", "not_found", ("artifact:notes.matrix@v1",))
    assert verify_content(_cell(unknown.as_json()), CELLS, evidence()) == ()


def test_unknown_needs_its_backing() -> None:
    assert _codes(_cell(Unknown("q?", "not_found").as_json())) == ["unknown_invalid"]
    unread = Unknown("q?", "not_found", ("record:pages/P-0001@1",))
    assert _codes(_cell(unread.as_json())) == ["unknown_invalid"]
    moved = Unknown("q?", "not_found", ("record:pages/P-0003@1",))
    reads = frozenset({"record:pages/P-0003@1"})
    assert _codes(_cell(moved.as_json()), reads=reads) == ["unknown_invalid"]
    assert _codes(_cell(Unknown("q?", "unverified").as_json())) == ["unknown_invalid"]
    failed = Unknown("q?", "unverified", failed_step="R-0001/gather/t02/c01")
    assert _codes(_cell(failed.as_json())) == []
    assert _codes(_cell({"unknown": {"question": "q?", "reason": "guess"}})) == ["unknown_invalid"]
    assert _codes(_cell(Unknown("q?", "not_checked").as_json())) == []


def test_unknown_outside_a_may_be_unknown_field_is_refused() -> None:
    note = {"cells": [{"vendor_note": Unknown("q?", "not_checked").as_json()}]}
    assert _codes(note) == ["unknown_not_allowed"]
    loose = {"summary": Unknown("q?", "not_checked").as_json()}
    assert _codes(loose) == ["unknown_not_allowed"]


def test_every_cite_is_verified_even_outside_a_policy() -> None:
    loose = {"summary": {"text": "x", "cite": {"span": "EX-0099", "quote": "nothing at all"}}}
    assert _codes(loose) == ["unknown_span"]
    malformed = {"summary": {"cite": {"span": "EX-0001"}}}
    assert _codes(malformed) == ["cite_invalid"]


def test_value_in_quote() -> None:
    """S3: the stored value appears in the quote exactly as the source states it."""
    quote = {"span": "EX-0001", "quote": "279 EUR per sensor"}
    assert _codes(_cell({"value": 279, "cite": quote})) == []
    assert _codes(_cell({"value": "279 euro", "cite": quote})) == ["value_not_in_quote"]
    assert _codes(_cell({"value": True, "cite": quote})) == ["value_not_in_quote"]
