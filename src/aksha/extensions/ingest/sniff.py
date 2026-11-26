"""Typing from content (design note 3.1, ``typed``): magic bytes first, the extension last.

:func:`sniff` reads the bytes, never the name, to decide what a file is:

1. Magic bytes: ``%PDF-``; a ZIP whose part names tell DOCX (``word/``), PPTX (``ppt/``)
   and XLSX (``xl/``) apart once ``[Content_Types].xml`` is present; an OLE compound file
   (legacy Office, or an encrypted Office file when it holds an ``EncryptedPackage``);
   PNG, JPEG, GIF, WebP (``RIFF....WEBP``), BMP, TIFF (``II*`` and ``MM*``) and the HEIC and
   HEIF ``ftyp`` brands.
2. Text: a byte-order mark (BOM) first, then a strict UTF-8 decode, then the declared
   fallback charset. Bytes that decode only as noise (NUL or other control bytes) are
   ``unidentified``. Within text: an HTML root, an SVG root, a strict JSON parse, RFC 5322
   header lines (an ``.eml``), and only then the extension, as a tie-breaker between
   plain text, Markdown and CSV.

The ZIP caps (summed uncompressed size and entry count, from the central directory) run
here, on the detected type, before any converter opens a part. ``zipfile`` never inflates
an entry past its declared size, so the declared sizes bound what a converter can read.
When the extension or the claimed type disagrees with the bytes, the bytes win and the
disagreement is returned as ``type_conflict``.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass

from aksha.extensions.ingest.types import Caps, Gap

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ZIP = "application/zip"
OLE = "application/x-ole-storage"
EMAIL = "message/rfc822"
HTML = "text/html"
SVG = "image/svg+xml"
JSON = "application/json"
CSV = "text/csv"
TSV = "text/tab-separated-values"
MARKDOWN = "text/markdown"
TEXT = "text/plain"
PNG = "image/png"
JPEG = "image/jpeg"
GIF = "image/gif"
WEBP = "image/webp"
BMP = "image/bmp"
TIFF = "image/tiff"
HEIC = "image/heic"
HEIF = "image/heif"
OCTET = "application/octet-stream"

IMAGE_TYPES: frozenset[str] = frozenset({PNG, JPEG, GIF, WEBP, BMP, TIFF, HEIC, HEIF})
"""Raster types that go through image intake (SVG is XML source, read as text)."""
TEXT_TYPES: frozenset[str] = frozenset({TEXT, MARKDOWN, CSV, TSV, JSON, HTML, SVG, EMAIL})
"""Types read from decoded text."""
OFFICE_TYPES: frozenset[str] = frozenset({DOCX, PPTX, XLSX})
SUPPORTED: frozenset[str] = frozenset({PDF, *OFFICE_TYPES, *IMAGE_TYPES, *TEXT_TYPES})
"""Every type a converter is declared for: the default allow-list."""

LABELS: dict[str, str] = {
    PDF: "PDF", DOCX: "Word document", PPTX: "PowerPoint deck", XLSX: "Excel workbook",
    ZIP: "ZIP archive", OLE: "legacy Office file", EMAIL: "email", HTML: "HTML page",
    SVG: "SVG drawing", JSON: "JSON", CSV: "CSV table", TSV: "tab-separated table",
    MARKDOWN: "Markdown", TEXT: "plain text", PNG: "PNG image", JPEG: "JPEG image",
    GIF: "GIF image", WEBP: "WebP image", BMP: "BMP image", TIFF: "TIFF image",
    HEIC: "HEIC image", HEIF: "HEIF image", OCTET: "unidentified bytes",
}  # fmt: skip
"""What a header calls each type."""

_EXTENSIONS: dict[str, str] = {
    "pdf": PDF, "docx": DOCX, "pptx": PPTX, "xlsx": XLSX, "zip": ZIP, "doc": OLE,
    "xls": OLE, "ppt": OLE, "msg": OLE, "eml": EMAIL, "html": HTML, "htm": HTML, "svg": SVG,
    "json": JSON, "csv": CSV, "tsv": TSV, "md": MARKDOWN, "markdown": MARKDOWN, "txt": TEXT,
    "log": TEXT, "png": PNG, "jpg": JPEG, "jpeg": JPEG, "gif": GIF, "webp": WEBP, "bmp": BMP,
    "tif": TIFF, "tiff": TIFF, "heic": HEIC, "heif": HEIF,
}  # fmt: skip
_HEIC_BRANDS = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis"})
_HEIF_BRANDS = frozenset({b"mif1", b"msf1", b"heif"})
_HEADER_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):[ \t]?.*$")
_MAIL_HEADERS = frozenset({"from", "to", "subject", "date", "message-id", "mime-version", "cc"})
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class Sniffed:
    """What the bytes are: the detected type, the text and its charset, any conflict."""

    detected_type: str
    charset: str | None = None
    text: str | None = None
    type_conflict: str | None = None
    gap: Gap | None = None


def extension_type(name: str) -> str | None:
    """The type ``name``'s extension claims, or None when it has none we know."""
    base = name.rsplit("/", 1)[-1]
    if "." not in base:
        return None
    return _EXTENSIONS.get(base.rsplit(".", 1)[-1].lower())


def decode_text(data: bytes, fallback: str) -> tuple[str, str] | None:
    """``(text, charset label)`` by BOM, then strict UTF-8, then ``fallback``; None for noise."""
    boms = ((b"\xef\xbb\xbf", "utf-8-sig", "utf-8 (BOM)"),
            (b"\xff\xfe\x00\x00", "utf-32-le", "utf-32-le (BOM)"),
            (b"\x00\x00\xfe\xff", "utf-32-be", "utf-32-be (BOM)"),
            (b"\xff\xfe", "utf-16-le", "utf-16-le (BOM)"),
            (b"\xfe\xff", "utf-16-be", "utf-16-be (BOM)"))  # fmt: skip
    for bom, codec, label in boms:
        if data.startswith(bom):
            try:
                body = data[len(bom) :].decode(codec.replace("-sig", ""), errors="strict")
            except UnicodeDecodeError:
                return None
            return (body, label) if not _noisy(body) else None
    try:
        text = data.decode("utf-8", errors="strict")
        return (text, "utf-8") if not _noisy(text) else None
    except UnicodeDecodeError:
        pass
    try:
        text = data.decode(fallback, errors="strict")
    except (UnicodeDecodeError, LookupError):
        return None
    return (text, f"{fallback} (fallback)") if not _noisy(text) else None


def _noisy(text: str) -> bool:
    """Whether ``text`` holds control characters no text file has (a NUL, a bell)."""
    return _CONTROL.search(text) is not None


def _zip_type(data: bytes, caps: Caps) -> Sniffed:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        return Sniffed(ZIP, gap=Gap("unsupported", "a ZIP archive that does not open"))
    if len(infos) > caps.max_zip_entries:
        detail = f"decompression_cap: {len(infos)} entries, over {caps.max_zip_entries}"
        return Sniffed(ZIP, gap=Gap("refused", detail))
    total = sum(i.file_size for i in infos)
    names = {i.filename for i in infos}
    kind = ZIP
    if "[Content_Types].xml" in names:
        if any(n.startswith("word/") for n in names):
            kind = DOCX
        elif any(n.startswith("ppt/") for n in names):
            kind = PPTX
        elif any(n.startswith("xl/") for n in names):
            kind = XLSX
    if total > caps.max_zip_bytes:
        detail = f"decompression_cap: expands to {total} bytes, over {caps.max_zip_bytes}"
        return Sniffed(kind, gap=Gap("refused", detail))
    return Sniffed(kind)


def _magic(data: bytes, caps: Caps) -> Sniffed | None:
    head = data[:1024]
    if b"%PDF-" in head:
        return Sniffed(PDF)
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return _zip_type(data, caps)
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        if "EncryptedPackage".encode("utf-16-le") in data:
            return Sniffed(OLE, gap=Gap("locked", "an encrypted Office file"))
        return Sniffed(OLE, gap=Gap("unsupported", "legacy Office format (.doc, .xls, .ppt, "
                                    ".msg): no converter declared"))  # fmt: skip
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return Sniffed(PNG)
    if data.startswith(b"\xff\xd8\xff"):
        return Sniffed(JPEG)
    if data.startswith((b"GIF87a", b"GIF89a")):
        return Sniffed(GIF)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return Sniffed(WEBP)
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return Sniffed(TIFF)
    if data[4:8] == b"ftyp":
        brands = {data[8:12]} | {data[i : i + 4] for i in range(16, min(len(data), 64), 4)}
        if brands & _HEIC_BRANDS:
            return Sniffed(HEIC)
        if brands & _HEIF_BRANDS:
            return Sniffed(HEIF)
        return Sniffed(OCTET, gap=Gap("unsupported", "an ISO media file (video or AVIF): no "
                                      "converter declared"))  # fmt: skip
    bmp_headers = (b"\x28\x00\x00\x00", b"\x0c\x00\x00\x00", b"\x7c\x00\x00\x00",
                   b"\x6c\x00\x00\x00")  # fmt: skip
    if data.startswith(b"BM") and len(data) > 26 and data[14:18] in bmp_headers:
        return Sniffed(BMP)
    return None


def _strip_lead(text: str) -> str:
    """``text`` without leading whitespace and XML or HTML comments and declarations."""
    s = text.lstrip()
    while True:
        if s.startswith("<!--"):
            end = s.find("-->")
            if end < 0:
                return s
            s = s[end + 3 :].lstrip()
        elif s.startswith("<?xml"):
            end = s.find("?>")
            if end < 0:
                return s
            s = s[end + 2 :].lstrip()
        else:
            return s


def _looks_like_email(text: str) -> bool:
    seen: set[str] = set()
    for line in text.splitlines()[:40]:
        if not line.strip():
            break
        if line[:1] in (" ", "\t"):
            continue
        m = _HEADER_LINE.match(line)
        if m is None:
            return False
        seen.add(m.group(1).lower())
    return len(seen & _MAIL_HEADERS) >= 2 and bool(seen & {"from", "to"})


def _text_type(text: str, claimed: str | None) -> tuple[str, str | None]:
    """The type of decoded ``text`` and a conflict note, the extension breaking ties."""
    lead = _strip_lead(text)[:200].lower()
    if lead.startswith("<!doctype html") or lead.startswith("<html"):
        return HTML, None
    if lead.startswith("<svg") or (lead.startswith("<!doctype svg")):
        return SVG, None
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            json.loads(stripped)
            return JSON, None
        except (ValueError, RecursionError):  # nesting past the parser's depth is not JSON
            if claimed == JSON:
                return TEXT, "extension .json, bytes not valid JSON"
    if _looks_like_email(text):
        return EMAIL, None
    if claimed in (MARKDOWN, CSV, TSV, TEXT):
        return claimed, None
    if claimed == JSON:
        return TEXT, "extension .json, bytes not valid JSON"
    return TEXT, None


def sniff(
    data: bytes, name: str, declared_type: str | None = None, *, caps: Caps | None = None
) -> Sniffed:
    """What ``data`` is (module doc); ``gap`` is set for an empty, unknown or capped file."""
    caps = caps or Caps()
    claimed = declared_type or extension_type(name)
    if not data:
        return Sniffed(OCTET, gap=Gap("unsupported", "empty"))
    found = _magic(data, caps)
    if found is None:
        decoded = decode_text(data, caps.fallback_charset)
        if decoded is None:
            return Sniffed(OCTET, gap=Gap("unsupported", "unidentified"),
                           type_conflict=_conflict(claimed, OCTET))  # fmt: skip
        text, charset = decoded
        kind, note = _text_type(text, claimed)
        conflict = note or _conflict(claimed, kind)
        return Sniffed(kind, charset=charset, text=text, type_conflict=conflict)
    conflict = _conflict(claimed, found.detected_type)
    return Sniffed(found.detected_type, gap=found.gap, type_conflict=conflict)


def _conflict(claimed: str | None, detected: str) -> str | None:
    if claimed is None or claimed == detected:
        return None
    if {claimed, detected} <= {TEXT, MARKDOWN, CSV, TSV}:
        return None
    if claimed == TEXT and detected in TEXT_TYPES:  # ``.txt`` says nothing more than "text"
        return None
    if {claimed, detected} == {HEIC, HEIF}:
        return None
    return f"claimed {LABELS.get(claimed, claimed)}, bytes are {LABELS.get(detected, detected)}"
