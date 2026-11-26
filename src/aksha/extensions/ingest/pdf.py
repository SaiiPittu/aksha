"""PDF at the fast rung: pypdf's text layer, one ``[p.N]`` anchor per page.

pypdf (BSD-3-Clause) comes with the optional extra ``aksha-harness[ingest]``; without it a
PDF is a gap ``unsupported`` that names the extra. In order:

1. An encrypted PDF that does not open with the empty user password is ``locked``.
2. A page count over ``Caps.max_pages`` is ``refused`` (``decompression_cap``); pypdf's own
   decoded-stream limits (75 MB per stream by default) raise ``LimitReachedError``, which is
   ``refused`` with the same reason.
3. Each page is read on its own and judged by two signals, code only: characters (other
   than whitespace) under ``Caps.min_chars_per_page`` reads as a scan; a share of
   replacement characters over ``Caps.max_garbled_ppm`` reads as a broken font encoding.
   No higher rung (layout or OCR) is declared in this build, so a failing page is dropped
   and named; a PDF whose every page fails is ``unreadable: OCR not available``.

The representation joins the kept pages as ``[p.N]`` followed by the page's text; each
anchor ``p.N`` spans that page's text, so a quote's offset says which page it is on.
"""

from __future__ import annotations

import io
import logging

from aksha.extensions.ingest.types import Anchor, Caps, Converted, Gap, PageRung, Representation

EXTRA = "aksha-harness[ingest]"


def _ranges(pages: list[int]) -> str:
    """``[7, 8, 9, 10, 12]`` as ``"7-10, 12"``."""
    out: list[str] = []
    run: list[int] = []
    for p in pages:
        if run and p == run[-1] + 1:
            run.append(p)
            continue
        if run:
            out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
        run = [p]
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ", ".join(out)


def page_label(pages: list[int]) -> str:
    """``"page 3"`` or ``"pages 7-10"``."""
    return ("page " if len(pages) == 1 else "pages ") + _ranges(pages)


def convert(data: bytes, caps: Caps | None = None) -> Converted:
    """The PDF's representation, or a gap (module doc)."""
    caps = caps or Caps()
    try:
        import pypdf
        from pypdf.errors import (
            DependencyError,
            FileNotDecryptedError,
            LimitReachedError,
            PdfReadError,
        )
    except ImportError:
        return Converted(gap=Gap("unsupported", f"PDF reading needs the {EXTRA} extra (pypdf)"))
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    steps = ("parse_fast",)
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                opened = bool(reader.decrypt(""))
            except (DependencyError, FileNotDecryptedError, NotImplementedError, PdfReadError):
                opened = False
            if not opened:
                return Converted(gap=Gap("locked", "an encrypted PDF (password protected)",
                                         steps))  # fmt: skip
        count = len(reader.pages)
        if count > caps.max_pages:
            detail = f"decompression_cap: {count} pages, over {caps.max_pages}"
            return Converted(gap=Gap("refused", detail, steps))
        texts: list[str] = []
        for page in reader.pages:
            texts.append(page.extract_text() or "")
    except LimitReachedError as e:
        return Converted(gap=Gap("refused", f"decompression_cap: {e}", steps))
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as e:
        return Converted(gap=Gap("unreadable", f"the PDF does not parse: {type(e).__name__}",
                                 steps))  # fmt: skip
    version = f"pypdf {pypdf.__version__}"
    parts: list[str] = []
    anchors: list[Anchor] = []
    pages: list[PageRung] = []
    signals: list[tuple[str, int]] = []
    scanned: list[int] = []
    garbled: list[int] = []
    at = 0
    for n, text in enumerate(texts, start=1):
        chars = sum(1 for c in text if not c.isspace())
        bad = text.count("�")
        ppm = 0 if chars == 0 else bad * 1_000_000 // chars
        signals.append((f"p.{n}.chars", chars))
        if chars < caps.min_chars_per_page:
            scanned.append(n)
            continue
        if ppm > caps.max_garbled_ppm:
            garbled.append(n)
            continue
        label = f"[p.{n}]\n"
        body = text.strip("\n") + "\n\n"
        start = at + len(label)
        parts.append(label + body)
        anchors.append(Anchor(f"p.{n}", start, start + len(body.rstrip("\n"))))
        pages.append(PageRung(f"p.{n}", "parse_fast", "extracted"))
        at += len(label) + len(body)
    if not pages:
        reason = "OCR not available" if scanned else "broken font encoding; OCR not available"
        return Converted(gap=Gap("unreadable", f"every page is a scan or unreadable; {reason}",
                                 steps))  # fmt: skip
    dropped: list[str] = []
    if scanned:
        dropped.append(f"{page_label(scanned)}: scanned, not read; OCR not available")
    if garbled:
        dropped.append(f"{page_label(garbled)}: broken font encoding, not read")
    kept = [int(p.anchor[2:]) for p in pages]
    notes = (f"{page_label(kept)} text layer by {version}",)
    rep = Representation("".join(parts), tuple(anchors), (version,), tuple(pages),
                         tuple(dropped), tuple(signals), page_count=len(texts),
                         notes=notes)  # fmt: skip
    return Converted(representation=rep)
