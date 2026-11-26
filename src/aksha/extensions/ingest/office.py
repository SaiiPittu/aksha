"""Word, PowerPoint and Excel files at the fast rung (all MIT, behind ``[ingest]``).

- **DOCX** through python-docx: paragraphs and tables in body order, each table as pipe
  rows (``| a | b |``), one anchor per heading (a ``Heading N`` or ``Title`` style).
- **PPTX** through python-pptx: one ``[slide N]`` anchor per slide, then its shapes' text
  in order (tables as pipe rows), then its speaker notes after ``notes:``.
- **XLSX** through openpyxl in read-only mode with ``data_only=True``: a formula cell shows
  the value the file cached when it was last saved, and no formula is ever evaluated. Each
  sheet starts ``[sheet Sheet1]``; each block of ``Caps.rows_per_anchor`` rows is an anchor
  ``Sheet1!A1:F200``; past ``Caps.max_cells`` cells in a sheet, or ``Caps.max_book_cells``
  in the workbook, the remaining rows (and sheets) are dropped and named. An empty row
  counts as one cell, so a sheet that declares a million blank rows cannot loop for long.

A missing library is a gap ``unsupported`` that names the extra; a file the library cannot
open is ``unreadable``. The ZIP caps already ran on the detected type (``sniff``).
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence
from typing import Any

from aksha.extensions.ingest.text import Names
from aksha.extensions.ingest.types import Anchor, Caps, Converted, Gap, PageRung, Representation

EXTRA = "aksha-harness[ingest]"


def _missing(what: str, lib: str) -> Converted:
    return Converted(gap=Gap("unsupported", f"{what} reading needs the {EXTRA} extra ({lib})"))


def _unreadable(what: str, e: Exception) -> Converted:
    return Converted(gap=Gap("unreadable", f"the {what} does not open: {type(e).__name__}",
                             ("parse_fast",)))  # fmt: skip


def _cell(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _pipe(cells: Iterable[str]) -> str:
    return "| " + " | ".join(cells) + " |"


class _Builder:
    """Text built in order, with anchors at line starts."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.size = 0
        self.anchors: list[Anchor] = []
        self.names = Names()

    def line(self, text: str) -> int:
        start = self.size
        self.parts.append(text + "\n")
        self.size += len(text) + 1
        return start

    def mark(self, label: str) -> None:
        self.anchors.append(Anchor(self.names.take(label), self.size, self.size))

    def done(self) -> tuple[str, tuple[Anchor, ...]]:
        text = "".join(self.parts)
        out: list[Anchor] = []
        for i, a in enumerate(self.anchors):
            end = self.anchors[i + 1].start if i + 1 < len(self.anchors) else len(text)
            out.append(Anchor(a.label, a.start, end))
        return text, tuple(out)


def docx(data: bytes) -> Converted:
    """A Word document's text, headings as anchors, tables as pipe rows."""
    try:
        import docx as python_docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError:
        return _missing("Word", "python-docx")
    try:
        document = python_docx.Document(io.BytesIO(data))
        out = _Builder()
        tables = 0
        for child in document.element.body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                p = Paragraph(child, document)
                text = " ".join(p.text.split())
                style = (p.style.name if p.style is not None else "") or ""
                if text and (style.startswith("Heading") or style == "Title"):
                    out.mark(text)
                if text:
                    out.line(text)
            elif tag == "tbl":
                tables += 1
                for row in Table(child, document).rows:
                    out.line(_pipe(_cell(c.text) for c in row.cells))
    except MemoryError:  # the worker's memory cap: parser_memory, not a damaged file
        raise
    except Exception as e:  # python-docx raises many types on a damaged file
        return _unreadable("Word document", e)
    text, anchors = out.done()
    notes = ["headings as anchors"]
    if tables:
        notes.append(f"{tables} tables as rows")
    version = f"python-docx {getattr(python_docx, '__version__', '?')}"
    rep = Representation(text, anchors, (version,), (PageRung("body", "parse_fast", "extracted"),),
                         notes=tuple(notes))  # fmt: skip
    return Converted(representation=rep)


def pptx(data: bytes) -> Converted:
    """A deck's slides as ``[slide N]`` anchors: titles, body text, tables, notes."""
    try:
        import pptx as python_pptx
    except ImportError:
        return _missing("PowerPoint", "python-pptx")
    try:
        deck = python_pptx.Presentation(io.BytesIO(data))
        out = _Builder()
        pages: list[PageRung] = []
        count = 0
        for n, slide in enumerate(deck.slides, start=1):
            count = n
            out.mark(f"slide {n}")
            out.line(f"[slide {n}]")
            pages.append(PageRung(f"slide {n}", "parse_fast", "extracted"))
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        text = " ".join("".join(r.text for r in para.runs).split())
                        if text:
                            out.line(text)
                if getattr(shape, "has_table", False) and shape.has_table:
                    for row in shape.table.rows:
                        out.line(_pipe(_cell(c.text) for c in row.cells))
            if slide.has_notes_slide:
                notes_text = " ".join(slide.notes_slide.notes_text_frame.text.split())
                if notes_text:
                    out.line(f"notes: {notes_text}")
    except MemoryError:  # the worker's memory cap: parser_memory, not a damaged file
        raise
    except Exception as e:  # python-pptx raises many types on a damaged file
        return _unreadable("PowerPoint deck", e)
    text, anchors = out.done()
    version = f"python-pptx {getattr(python_pptx, '__version__', '?')}"
    rep = Representation(text, anchors, (version,), tuple(pages), page_count=count,
                         notes=("slides as anchors, speaker notes kept",))  # fmt: skip
    return Converted(representation=rep)


def _column(n: int) -> str:
    """1 -> ``A``, 27 -> ``AA``."""
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def xlsx(data: bytes, caps: Caps | None = None) -> Converted:
    """A workbook's sheets with cached values, row blocks as ``Sheet1!A1:F200`` anchors."""
    caps = caps or Caps()
    try:
        import openpyxl
    except ImportError:
        return _missing("Excel", "openpyxl")
    out = _Builder()
    pages: list[PageRung] = []
    dropped: list[str] = []
    try:
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            left = caps.max_book_cells
            for s, sheet in enumerate(book.worksheets, start=1):
                title = sheet.title
                if left <= 0:
                    names = [w.title for w in book.worksheets[s - 1 :]]
                    dropped.append(f"sheets {', '.join(names)[:200]}: over the "
                                   f"{caps.max_book_cells}-cell workbook cap")  # fmt: skip
                    break
                out.line(f"[sheet {title}]")
                pages.append(PageRung(f"sheet {title}", "parse_fast", "extracted"))
                cells = 0
                block: list[tuple[int, Sequence[Any]]] = []
                for r, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                    cost = max(len(row), 1)  # an empty row still costs a step: rows are capped
                    if cells + cost > min(caps.max_cells, left):
                        cap = (f"{caps.max_cells}-cell cap" if cells + cost > caps.max_cells
                               else f"{caps.max_book_cells}-cell workbook cap")  # fmt: skip
                        dropped.append(f"sheet {title}: rows from {r} on, over the {cap}")
                        break
                    cells += cost
                    block.append((r, row))
                    if len(block) == caps.rows_per_anchor:
                        _flush(out, title, block)
                        block = []
                if block:
                    _flush(out, title, block)
                left -= cells
        finally:
            book.close()
    except MemoryError:  # the worker's memory cap: parser_memory, not a damaged file
        raise
    except Exception as e:  # openpyxl raises many types on a damaged file
        return _unreadable("Excel workbook", e)
    text, anchors = out.done()
    version = f"openpyxl {openpyxl.__version__}"
    notes = ("cached values; formulas never evaluated", "row blocks as anchors")
    rep = Representation(text, anchors, (version,), tuple(pages), tuple(dropped),
                         page_count=len(pages), notes=notes)  # fmt: skip
    return Converted(representation=rep)


def _flush(out: _Builder, title: str, block: list[tuple[int, Sequence[Any]]]) -> None:
    rows = [(r, [_cell(v) for v in row]) for r, row in block]
    width = max((max((i + 1 for i, v in enumerate(vals) if v), default=0) for _, vals in rows),
                default=0)  # fmt: skip
    if width == 0:
        return
    first, last = rows[0][0], rows[-1][0]
    out.mark(f"{title}!A{first}:{_column(width)}{last}")
    for r, vals in rows:
        if any(vals):
            out.line(f"{r}: " + " | ".join(vals[:width]))
