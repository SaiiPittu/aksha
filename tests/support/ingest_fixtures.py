"""Synthetic ingestion fixtures, generated in code: no real document is checked in.

Every builder returns bytes. The PDF writer is a few lines of the PDF syntax (one
Helvetica font, one content stream per page), so a page with no text operator reads as a
scan. The Office, image and email builders use the same libraries the converters read
with (python-docx, python-pptx, openpyxl, Pillow) or the standard library.
"""

from __future__ import annotations

import io
import itertools
import struct
import zipfile
import zlib
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any

from aksha.extensions.ingest.ingest import Ingested, ingest
from aksha.extensions.ingest.types import Refusal


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_pdf(pages: list[str | None]) -> bytes:
    """A PDF with one page per item: its text as one or more lines, or None for a scan."""
    objects: list[bytes] = []
    n_pages = len(pages)
    font_id = 3 + 2 * n_pages
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n_pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        page = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} "
                f"0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>")  # fmt: skip
        objects.append(page.encode())
        if text is None:
            stream = b"0.9 g 0 0 612 792 re f"
        else:
            ops = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
            for line in text.split("\n"):
                ops.append(f"({_pdf_escape(line)}) Tj T*")
            ops.append("ET")
            stream = "\n".join(ops).encode("latin-1")
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
              .encode())  # fmt: skip
    return out.getvalue()


def encrypted_pdf(password: str = "s3cret") -> bytes:
    """A text PDF encrypted with a user password."""
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(text_pdf(["Confidential terms"])))
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(password, algorithm="RC4-128")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def docx_bytes() -> bytes:
    """A Word document: a title, two headings, paragraphs and one table."""
    import docx

    d = docx.Document()
    d.add_heading("Supplier review", level=0)
    d.add_heading("Pricing", level=1)
    d.add_paragraph("The base plan costs 40 EUR per seat per month.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Plan", "Price"
    t.cell(1, 0).text, t.cell(1, 1).text = "Base", "40 EUR"
    d.add_heading("Risks", level=1)
    d.add_paragraph("Delivery slips past the second quarter.")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def pptx_bytes() -> bytes:
    """A two-slide deck with titles, body text and speaker notes on slide 2."""
    from pptx import Presentation

    deck = Presentation()
    s1 = deck.slides.add_slide(deck.slide_layouts[1])
    s1.shapes.title.text = "Quarterly plan"
    s1.placeholders[1].text = "Ship the sensor kit"
    s2 = deck.slides.add_slide(deck.slide_layouts[1])
    s2.shapes.title.text = "Budget"
    s2.placeholders[1].text = "Hardware 120k"
    s2.notes_slide.notes_text_frame.text = "Mention the supplier delay"
    buf = io.BytesIO()
    deck.save(buf)
    return buf.getvalue()


def xlsx_bytes(rows: int = 3) -> bytes:
    """A workbook whose ``C2`` is a formula ``=A2*B2`` with the cached value 120."""
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Costs"
    sheet.append(["qty", "unit", "total"])
    sheet.append([3, 40, "=A2*B2"])
    for i in range(rows - 2):
        sheet.append([i, 1, i])
    buf = io.BytesIO()
    book.save(buf)
    return _cache_formula(buf.getvalue(), "C2", "120")


def _cache_formula(data: bytes, cell: str, value: str) -> bytes:
    """Rewrite a sheet so formula ``cell`` carries a cached ``value``, as a saved file does."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            body = src.read(info.filename)
            if info.filename.startswith("xl/worksheets/sheet"):
                text = body.decode("utf-8")
                start = text.index(f'<c r="{cell}"')
                end = text.index("</c>", start)
                fragment = text[start:end]
                formula = fragment[fragment.index("<f>") : fragment.index("</f>") + 4]
                text = text[:start] + f'<c r="{cell}">{formula}<v>{value}</v>' + text[end:]
                body = text.encode("utf-8")
            dst.writestr(info, body)
    return out.getvalue()


def zip_bomb(entry_bytes: int = 5 * 1024 * 1024) -> bytes:
    """A ZIP that looks like a DOCX and expands one part to ``entry_bytes`` zeros."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", b"\0" * entry_bytes)
    return out.getvalue()


def plain_zip() -> bytes:
    """A ZIP of one text file."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("notes.txt", "hello")
    return out.getvalue()


def eml_bytes(attachment: bytes | None = None, name: str = "terms.pdf") -> bytes:
    """An email with a plain body and, optionally, one attachment."""
    m = EmailMessage()
    m["From"] = "Ana Ruiz <ana@example.test>"
    m["To"] = "team@example.test"
    m["Date"] = "Tue, 24 Nov 2026 09:30:00 +0000"
    m["Subject"] = "Revised terms"
    m.set_content("Hi team,\nThe revised terms are attached.\nAna\n")
    if attachment is not None:
        main, sub = ("application", "pdf") if name.endswith(".pdf") else ("message", "rfc822")
        if main == "message":
            inner = EmailMessage()
            from email import policy
            from email.parser import BytesParser

            inner = BytesParser(policy=policy.default).parsebytes(attachment)
            m.add_attachment(inner, filename=name)
        else:
            m.add_attachment(attachment, maintype=main, subtype=sub, filename=name)
    return bytes(m)


def nested_eml(depth: int) -> bytes:
    """An email that forwards an email ``depth`` levels deep, the innermost with a PDF."""
    inner = eml_bytes(text_pdf(["Innermost terms: 30 days"]))
    for _ in range(depth):
        inner = eml_bytes(inner, name="forwarded.eml")
    return inner


def jpeg_rotated_with_gps(w: int = 64, h: int = 32) -> bytes:
    """A ``w`` by ``h`` JPEG, left half red, EXIF orientation 6 and a GPS position."""
    from PIL import Image

    im = Image.new("RGB", (w, h), (0, 0, 255))
    for x in range(w // 2):
        for y in range(h):
            im.putpixel((x, y), (255, 0, 0))
    exif = Image.Exif()
    exif[0x0112] = 6
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (52.0, 22.0, 1.0)
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes(), quality=95)
    return buf.getvalue()


def png_bytes(w: int = 40, h: int = 30, color: tuple[int, int, int] = (10, 120, 200)) -> bytes:
    """A plain PNG with no metadata."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def sized_png(w: int, h: int) -> bytes:
    """A PNG of ``w`` by ``h`` (a gradient, so the encoder has real work)."""
    from PIL import Image

    im = Image.linear_gradient("L").resize((w, h)).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def animated_gif(frames: int = 4) -> bytes:
    """An animated GIF of ``frames`` frames."""
    from PIL import Image

    images = [Image.new("RGB", (20, 20), (i * 60, 255 - i * 60, 0)) for i in range(frames)]
    buf = io.BytesIO()
    images[0].save(buf, "GIF", save_all=True, append_images=images[1:], duration=100, loop=0)
    return buf.getvalue()


def multipage_tiff(pages: int = 3) -> bytes:
    """A TIFF of ``pages`` pages."""
    from PIL import Image

    images = [Image.new("RGB", (30 + i, 20), (i * 60, 0, 0)) for i in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, "TIFF", save_all=True, append_images=images[1:])
    return buf.getvalue()


def png_header_only(w: int, h: int) -> bytes:
    """A PNG signature and an IHDR claiming ``w`` by ``h``, with no pixel data at all."""
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)

    def chunk_of(kind: bytes, body: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        return struct.pack(">I", len(body)) + kind + body + crc

    idat = zlib.compress(b"\0" * 16)
    return (b"\x89PNG\r\n\x1a\n" + chunk_of(b"IHDR", ihdr) + chunk_of(b"IDAT", idat)
            + chunk_of(b"IEND", b""))  # fmt: skip


def heic_bytes() -> bytes | None:
    """A small HEIC image, or None when pillow-heif is not installed."""
    try:
        import pillow_heif
    except ImportError:
        return None
    from PIL import Image

    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.new("RGB", (48, 32), (0, 200, 0)).save(buf, "HEIF")
    return buf.getvalue()


# --- helpers ---------------------------------------------------------------------------------


def need(module: str) -> None:
    """Skip the calling test when the optional library ``module`` is not installed."""
    import importlib.util

    import pytest

    if importlib.util.find_spec(module) is None:
        pytest.skip(f"{module} is not installed (aksha-harness[ingest])")


def keys() -> Callable[[str, int, str, bytes], tuple[str, int]]:
    """Doc keys ``D-0002`` onwards for children, each at version 1."""
    counter = itertools.count(2)
    return lambda parent, n, name, data: (f"D-{next(counter):04d}", 1)


def run(name: str, data: bytes, **kw: Any) -> Ingested:
    """Ingest ``data`` as ``D-0001`` and insist nothing was refused before storing."""
    got = ingest(name, data, doc_key="D-0001", key_for=keys(), **kw)
    assert not isinstance(got, Refusal), got
    return got
