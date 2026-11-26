"""Groups 1 to 5: every format's representation and anchors, from synthetic fixtures."""

from __future__ import annotations

from aksha.extensions.ingest import sniff
from aksha.extensions.ingest.types import Caps
from tests.support import ingest_fixtures as fx

# --- 1, 2. PDF ---------------------------------------------------------------------------


def test_a_quote_sits_at_the_recorded_page_3_anchor() -> None:
    fx.need("pypdf")
    pages: list[str | None] = [
        "Cover page of the agreement",
        "Definitions and scope apply",
        "Clause 7: payment is due within 30 days",
        "Signatures of both parties follow",
    ]
    data = fx.text_pdf(pages)
    got = fx.run("terms.pdf", data)
    rep = got.representation
    assert rep is not None and got.gap is None
    p3 = rep.anchor("p.3")
    assert p3 is not None
    quote = "payment is due within 30 days"
    at = rep.text.index(quote)
    assert p3.start <= at and at + len(quote) <= p3.end
    assert rep.text[p3.start : p3.end] == "Clause 7: payment is due within 30 days"
    assert [a.label for a in rep.anchors] == ["p.1", "p.2", "p.3", "p.4"]
    assert {p.trust for p in rep.pages} == {"extracted"}
    assert rep.page_count == 4 and rep.converters[0].startswith("pypdf ")


def test_a_mixed_pdf_keeps_text_pages_and_names_the_scanned_ones() -> None:
    fx.need("pypdf")
    pages: list[str | None] = ["Text page one with enough words", None, None,
                               "Text page four with enough words"]  # fmt: skip
    got = fx.run("mixed.pdf", fx.text_pdf(pages))
    rep = got.representation
    assert rep is not None
    assert [a.label for a in rep.anchors] == ["p.1", "p.4"]
    assert rep.dropped == ("pages 2-3: scanned, not read; OCR not available",)
    assert "not included: pages 2-3: scanned, not read; OCR not available" in got.header
    assert "converted: pages 1, 4 text layer by pypdf" in got.header


def test_an_all_scanned_pdf_is_a_gap_that_says_ocr_is_not_available() -> None:
    fx.need("pypdf")
    got = fx.run("scan.pdf", fx.text_pdf([None, None, None]))
    assert got.representation is None and got.gap is not None
    assert got.gap.reason == "unreadable"
    assert "OCR not available" in got.gap.detail
    assert "not readable: unreadable" in got.header


def test_an_encrypted_pdf_is_locked() -> None:
    fx.need("pypdf")
    got = fx.run("locked.pdf", fx.encrypted_pdf())
    assert got.gap is not None and got.gap.reason == "locked"


def test_a_pdf_over_the_page_cap_is_refused() -> None:
    fx.need("pypdf")
    got = fx.run("long.pdf", fx.text_pdf(["page text here"] * 5), caps=Caps(max_pages=3))
    assert got.gap is not None and got.gap.reason == "refused"
    assert got.gap.detail.startswith("decompression_cap")


def test_a_pdf_without_pypdf_is_a_gap_naming_the_extra(monkeypatch: object) -> None:
    import builtins

    import pytest

    real = builtins.__import__

    def blocked(name: str, *a: object, **k: object) -> object:
        if name == "pypdf" or name.startswith("pypdf."):
            raise ImportError(name)
        return real(name, *a, **k)  # type: ignore[arg-type]

    mp = pytest.MonkeyPatch()
    mp.setattr(builtins, "__import__", blocked)
    try:
        got = fx.run("a.pdf", fx.text_pdf(["some text on the page"]))
    finally:
        mp.undo()
    assert got.gap is not None and got.gap.reason == "unsupported"
    assert "aksha-harness[ingest]" in got.gap.detail


# --- 3. Office and CSV -------------------------------------------------------------------


def test_docx_headings_are_anchors_and_tables_are_rows() -> None:
    fx.need("docx")
    got = fx.run("review.docx", fx.docx_bytes())
    rep = got.representation
    assert rep is not None
    assert [a.label for a in rep.anchors] == ["Supplier review", "Pricing", "Risks"]
    pricing = rep.anchor("Pricing")
    assert pricing is not None
    assert "40 EUR per seat" in rep.text[pricing.start : pricing.end]
    assert "| Base | 40 EUR |" in rep.text


def test_pptx_slides_are_anchors_with_notes() -> None:
    fx.need("pptx")
    got = fx.run("plan.pptx", fx.pptx_bytes())
    rep = got.representation
    assert rep is not None and rep.page_count == 2
    assert [a.label for a in rep.anchors] == ["slide 1", "slide 2"]
    s2 = rep.anchor("slide 2")
    assert s2 is not None
    body = rep.text[s2.start : s2.end]
    assert "Budget" in body and "notes: Mention the supplier delay" in body
    assert "2 slides" in got.header


def test_xlsx_has_sheet_range_anchors_and_cached_formula_values() -> None:
    fx.need("openpyxl")
    got = fx.run("costs.xlsx", fx.xlsx_bytes(rows=3))
    rep = got.representation
    assert rep is not None
    assert [a.label for a in rep.anchors] == ["Costs!A1:C3"]
    assert "2: 3 | 40 | 120" in rep.text  # the cached value, never the formula
    assert "A2*B2" not in rep.text
    assert "formulas never evaluated" in got.header


def test_xlsx_rows_past_the_cell_cap_are_dropped_by_name() -> None:
    fx.need("openpyxl")
    got = fx.run("big.xlsx", fx.xlsx_bytes(rows=20), caps=Caps(max_cells=15))
    rep = got.representation
    assert rep is not None
    assert rep.dropped == ("sheet Costs: rows from 6 on, over the 15-cell cap",)


def test_a_semicolon_csv_in_latin_1_is_decoded_and_anchored() -> None:
    data = "name;city\nJosé;Málaga\nAna;Köln\n".encode("latin-1")
    got = fx.run("people.csv", data)
    rep = got.representation
    assert rep is not None
    assert got.document.charset == "latin-1 (fallback)"
    assert "José;Málaga" in rep.text
    assert [a.label for a in rep.anchors] == ["rows 1-2"]
    assert "delimiter ';'" in got.header and "charset latin-1 (fallback)" in got.header
    assert not got.as_given


def test_csv_row_blocks_split_at_the_rows_per_anchor() -> None:
    body = "a,b\n" + "".join(f"{i},x\n" for i in range(5))
    rep = fx.run("t.csv", body.encode(), caps=Caps(rows_per_anchor=2)).representation
    assert rep is not None
    assert [a.label for a in rep.anchors] == ["rows 1-2", "rows 3-4", "rows 5-5"]
    first, last = rep.anchors[0], rep.anchors[-1]
    assert rep.text[first.start : first.end] == "a,b\n0,x\n1,x\n"
    assert rep.text[last.start : last.end] == "4,x\n"


def test_a_utf16_text_file_is_read_by_its_bom() -> None:
    got = fx.run("note.txt", "﻿hello there".encode("utf-16-le"))
    rep = got.representation
    assert rep is not None and rep.text == "hello there"
    assert got.document.charset == "utf-16-le (BOM)"


# --- 4. HTML, Markdown, JSON -------------------------------------------------------------


def test_html_drops_script_and_style_and_anchors_headings() -> None:
    page = (b"<!doctype html><html><head><title>Plans</title><style>p{}</style></head><body>"
            b"<h1>Pricing</h1><p>Base plan 40 EUR</p><script>steal()</script>"
            b"<h2>Support</h2><p>Email only</p></body></html>")  # fmt: skip
    got = fx.run("p.html", page)
    rep = got.representation
    assert rep is not None
    assert "steal()" not in rep.text and "p{}" not in rep.text
    assert [a.label for a in rep.anchors] == ["Pricing", "Support"]
    pricing = rep.anchor("Pricing")
    assert pricing is not None and "Base plan 40 EUR" in rep.text[pricing.start : pricing.end]
    assert rep.dropped == ("2 script or style blocks",)
    assert got.as_given  # UTF-8 HTML is served as it is; agents parse it themselves


def test_markdown_headings_are_anchors() -> None:
    text = "# Title\n\nintro\n\n## Part A\n\nalpha\n\n## Part B\n\nbeta\n"
    rep = fx.run("n.md", text.encode()).representation
    assert rep is not None
    assert [a.label for a in rep.anchors] == ["Title", "Part A", "Part B"]
    a = rep.anchor("Part A")
    assert a is not None and rep.text[a.start : a.end] == "## Part A\n\nalpha\n\n"
    title = rep.anchor("Title")
    assert title is not None and title.end == len(text)


def test_json_top_level_keys_are_anchors() -> None:
    rep = fx.run("k.json", b'{"price": 40, "plan": {"name": "base"}}').representation
    assert rep is not None
    assert [a.label for a in rep.anchors] == ["price", "plan"]
    plan = rep.anchor("plan")
    assert plan is not None and rep.text[plan.start :].startswith('"plan": {')


def test_invalid_json_is_plain_text_with_the_conflict_named() -> None:
    got = fx.run("broken.json", b'{"a": 1,')
    assert got.document.detected_type == sniff.TEXT
    assert got.document.type_conflict == "extension .json, bytes not valid JSON"
    assert got.representation is not None and got.representation.text == '{"a": 1,'


# --- 5. Email ----------------------------------------------------------------------------


def test_an_email_with_a_pdf_attachment_is_two_documents() -> None:
    fx.need("pypdf")
    data = fx.eml_bytes(fx.text_pdf(["Attached terms: net 30 days payment"]))
    got = fx.run("mail.eml", data)
    rep = got.representation
    assert rep is not None and got.document.detected_type == sniff.EMAIL
    assert rep.text.startswith("From: Ana Ruiz <ana@example.test>\nTo: team@example.test")
    assert "The revised terms are attached." in rep.text
    assert "1. terms.pdf (application/pdf" in rep.text
    (child,) = got.children
    assert child.document.origin == "attachment" and child.document.parent == "D-0001"
    assert child.document.detected_type == sniff.PDF
    assert child.representation is not None
    assert "net 30 days payment" in child.representation.text
    assert '"terms.pdf" (attachment of D-0001)' in child.header


def test_a_nested_email_past_the_depth_cap_is_refused_by_name() -> None:
    fx.need("pypdf")
    got = fx.run("fwd.eml", fx.nested_eml(3))
    chain = got.walk()
    assert [d.document.parent for d in chain] == [None, "D-0001", "D-0002"]
    deepest = chain[-1]
    (refusal,) = deepest.refusals
    assert (refusal.cap, refusal.value, refusal.limit) == ("max_depth", 3, 2)
    assert refusal.name_as_given == "forwarded.eml"
    assert deepest.representation is not None
    assert "attachments refused: 'forwarded.eml' (max_depth)" in deepest.representation.dropped
