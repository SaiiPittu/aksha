"""Group 6: typing from content, the allow-list on the detected type, the ZIP caps."""

from __future__ import annotations

from aksha.extensions.ingest import sniff
from aksha.extensions.ingest.types import Caps, Gap
from tests.support import ingest_fixtures as fx


def test_a_zip_renamed_pdf_is_judged_as_a_zip() -> None:
    got = fx.run("contract.pdf", fx.plain_zip())
    assert got.document.detected_type == sniff.ZIP
    assert got.document.type_conflict == "claimed PDF, bytes are ZIP archive"
    assert got.gap is not None and got.gap.reason == "refused"
    assert got.gap.detail == "type not allowed: ZIP archive"
    assert "type conflict: claimed PDF, bytes are ZIP archive" in got.header


def test_a_zip_bomb_is_refused_before_any_part_is_opened() -> None:
    got = fx.run("bomb.docx", fx.zip_bomb(), caps=Caps(max_zip_bytes=1024 * 1024))
    assert got.document.detected_type == sniff.DOCX
    assert got.gap is not None and got.gap.reason == "refused"
    assert got.gap.detail.startswith("decompression_cap: expands to 5242888 bytes")


def test_a_zip_with_too_many_entries_is_refused() -> None:
    found = sniff.sniff(fx.zip_bomb(10), "a.docx", caps=Caps(max_zip_entries=1))
    assert found.gap is not None and "2 entries" in found.gap.detail


def test_empty_and_unidentified_bytes_are_gaps() -> None:
    empty = fx.run("e.txt", b"")
    assert empty.gap is not None and (empty.gap.reason, empty.gap.detail) == (
        "unsupported",
        "empty",
    )
    noise = fx.run("r.bin", bytes(range(256)))
    assert noise.gap is not None and noise.gap.detail == "unidentified"


def test_office_types_are_told_apart_by_part_names() -> None:
    fx.need("docx")
    fx.need("pptx")
    fx.need("openpyxl")
    assert sniff.sniff(fx.docx_bytes(), "x").detected_type == sniff.DOCX
    assert sniff.sniff(fx.pptx_bytes(), "x").detected_type == sniff.PPTX
    assert sniff.sniff(fx.xlsx_bytes(), "x").detected_type == sniff.XLSX


def test_image_magic_and_heic_brands() -> None:
    fx.need("PIL")
    assert sniff.sniff(fx.png_bytes(), "x").detected_type == sniff.PNG
    assert sniff.sniff(fx.multipage_tiff(), "x").detected_type == sniff.TIFF
    assert sniff.sniff(fx.animated_gif(), "x").detected_type == sniff.GIF
    heif = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"
    assert sniff.sniff(heif, "x").detected_type == sniff.HEIC


def test_html_claimed_as_text_is_read_as_html_and_an_eml_by_its_headers() -> None:
    page = sniff.sniff(b"<!-- c -->\n<html><body>x</body></html>", "a.txt")
    assert page.detected_type == sniff.HTML
    mail = sniff.sniff(fx.eml_bytes(), "message.txt")
    assert mail.detected_type == sniff.EMAIL


def test_an_encrypted_office_file_is_locked_and_legacy_office_unsupported() -> None:
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 32
    assert sniff.sniff(ole + "EncryptedPackage".encode("utf-16-le"), "a.docx").gap == \
        Gap("locked", "an encrypted Office file")  # fmt: skip
    legacy = sniff.sniff(ole, "a.doc").gap
    assert legacy is not None and legacy.reason == "unsupported"


def test_a_converter_that_raises_is_a_gap_not_a_crash(monkeypatch: object) -> None:
    import pytest

    from aksha.extensions.ingest import text

    def boom(body: str) -> object:
        raise RecursionError("deep")

    mp = pytest.MonkeyPatch()
    mp.setattr(text, "markdown", boom)
    try:
        got = fx.run("n.md", b"# A\n")
    finally:
        mp.undo()
    assert got.gap is not None and got.gap.detail == "the converter failed: RecursionError"
