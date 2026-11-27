"""Structural segmentation of any text record into citable passages (ADR-P13 amendment G3).

A read tool that hands a record to an extraction alias first cuts it into passages by
structure alone, never by what one venture's files look like. Every passage is a range
``[start, end)`` of the record's raw text, so a span inside it is a span of the record.
Nothing here decides what a passage *means* (a price, a mechanism step, a review): that
is the extraction or classification alias's answer.

1. :func:`sniff_format` names a record's format from its content first (an HTML document,
   a JSON value, an email's headers, a delimited table, Markdown, plain text) and from its
   name only when the content is ambiguous, so an HTML page saved without an extension, or
   a semicolon export named ``.txt``, reads as what it is.
2. :func:`segment` cuts a record into :class:`Segment` values in document order (a
   delimited table: its ``header`` line with the column names, then one ``row`` each):

   * HTML, with the standard library's parser and exact offsets: ``meta`` tags (the whole
     tag, its attributes in ``attrs``) and the canonical link, the ``title``, structured
     data (``data``: a JSON-LD script's body), and every block of visible text
     (``heading``, ``paragraph``, ``item``, ``row`` with its cells). A block is the text
     between two block boundaries, so ``<span>€9</span><span>/mo</span>`` in a pricing
     card is one passage and a nested ``<div>`` never swallows its children. A block under
     ``display:none``, ``hidden`` or ``aria-hidden`` is ``hidden``; under a ``footer`` it
     is ``footer``. ``attrs["class"]`` keeps the classes of the block's element.
   * Text, Markdown and email: front matter and email headers (``meta``, ``attrs`` holding
     the key and the value's own range), headings (``#`` lines, a short bold line, a short
     line ending in a colon, ``Slide N:`` lines), list items (``-``, ``*``, ``+``, ``•``,
     ``1.``, ``1)``, ``a)``), pipe-table rows with their cells, and paragraphs: consecutive
     non-blank lines joined, so a hard-wrapped sentence stays whole.
3. :func:`read_rows` reads a table: delimited text (the delimiter sniffed among ``,``,
   ``;``, tab and ``|``, quoted cells across line breaks, each cell's raw range) or JSON
   (the largest list of objects anywhere in it, nested objects flattened to dotted keys,
   each string value's raw range from a position-tracking scanner).
4. :func:`parse_date` reads a date as people write it (ISO, ``14 Jan 2026``,
   ``March 21, 2026``, ``21.03.2026``, ``03/21/2026``) into ISO; an ambiguous all-numeric
   date (``03/04/2026``) is None unless the record's ``order`` says which comes first.
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from typing import Any

FORMATS = ("html", "markdown", "csv", "json", "email", "text")

BLOCK_TAGS = frozenset(
    [
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "br",
        "caption",
        "dd",
        "details",
        "dialog",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
        "button",
        "label",
        "option",
    ]
)
_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "head"})
_VOID = frozenset({"meta", "link", "br", "hr", "img", "input", "source", "wbr", "area", "col"})


@dataclass(frozen=True)
class Segment:
    """One structural passage: ``raw[start:end]`` is its exact text.

    ``text`` is what a reader sees (tags removed, whitespace folded). ``section`` is the
    nearest heading above it; ``level`` a heading's level; ``cells`` a row's cells as
    ``(text, start, end)``; ``attrs`` a meta tag's attributes or a block's classes.
    """

    kind: str
    start: int
    end: int
    text: str
    section: str = ""
    level: int = 0
    cells: tuple[tuple[str, int, int], ...] = ()
    attrs: Mapping[str, str] = field(default_factory=dict)


def visible(fragment: str) -> str:
    """``fragment`` with tags removed, the common entities decoded and whitespace folded."""
    text = re.sub(r"<[^>]+>", " ", fragment)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&euro;", "€"), ("&pound;", "£")):  # fmt: skip
        text = text.replace(a, b)
    return " ".join(text.split())


# --- format -------------------------------------------------------------------------------

_EMAIL_HEAD = re.compile(r"^(from|to|date|subject|cc|sent):[ \t]", re.I | re.M)
_HTML_HEAD = re.compile(r"^\s*(<!doctype html|<html[\s>]|<\?xml[^>]*>\s*<html)", re.I)
_HTML_TAGS = re.compile(r"<(p|div|body|table|h[1-6]|span|li|meta)[\s>/]", re.I)


def _delimiter(lines: Sequence[str]) -> str | None:
    """The delimiter every one of the first rows splits into the same number (> 1) of cells.

    Rows, not lines: a quoted cell that spans a line break joins its lines into one row,
    and a delimiter inside quotes is not counted.
    """
    rows: list[str] = []
    pending = ""
    for ln in lines:
        pending = f"{pending}\n{ln}" if pending else ln
        if pending.count('"') % 2 == 0:
            if pending.strip():
                rows.append(re.sub(r'"[^"]*"', '""', pending))
            pending = ""
        if len(rows) >= 8:
            break
    if len(rows) < 2:
        return None
    for skip in (0, 1, 2):  # a title line or two pasted above the header
        body = rows[skip:]
        if skip and len(body) < 3:
            break
        for d in ("\t", ";", ",", "|"):
            counts = {r.count(d) for r in body}
            if len(counts) == 1 and min(counts) >= 1:
                return d
    return None


def sniff_format(record_id: str, raw: str) -> str:
    """The format ``raw`` is in (module doc, item 1)."""
    head = raw.lstrip()[:4000]
    name = record_id.rsplit("/", 1)[-1].lower()
    if head.startswith("WEBVTT"):
        return "vtt"
    if _HTML_HEAD.match(head) or len(_HTML_TAGS.findall(head)) >= 3:
        return "html"
    if head.startswith("<?xml") or re.match(r"<[A-Za-z][\w.-]*(\s[^<>]*)?>", head):
        return "xml"
    if head[:1] in "[{":
        try:
            json.loads(raw)
            return "json"
        except ValueError:
            if _json_lines(raw):
                return "jsonl"
    first = head.split("\n\n", 1)[0]
    if len(_EMAIL_HEAD.findall(first)) >= 2:
        return "email"
    if name.endswith((".md", ".markdown")):
        return "markdown"
    if _yaml_like(raw):
        return "yaml"
    lines = head.splitlines()
    if name.endswith((".csv", ".tsv")) or (
        _delimiter(lines) is not None and not head.startswith(("#", "---", "|"))
        and not any(ln.startswith(("#", "- ", "* ")) for ln in lines[:8])
    ):  # fmt: skip
        return "csv"
    if head.startswith("---\n") or re.search(r"^#{1,6} |^\|.*\|\s*$", head, re.M):
        return "markdown"
    return "text"


def _json_lines(raw: str) -> bool:
    """Whether every non-blank line of ``raw`` is a JSON object, at least two of them."""
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    try:
        return all(isinstance(json.loads(ln), dict) for ln in lines)
    except ValueError:
        return False


_YAML_LINE = re.compile(r"^(\s*)(?:-\s+)?([A-Za-z_][\w .-]{0,40}):(?:\s+(.*?))?\s*$")
_YAML_ITEM = re.compile(r"^(\s*)-\s+(\S.*?)\s*$")


def _yaml_like(raw: str) -> bool:
    """Whether ``raw`` reads as YAML: keyed lines and list items, some nested, no prose."""
    lines = [ln for ln in raw.splitlines()
             if ln.strip() and not ln.lstrip().startswith("#") and ln.strip() != "---"]  # fmt: skip
    if len(lines) < 4:
        return False
    keyed = [ln for ln in lines if _YAML_LINE.match(ln)]
    shaped = [ln for ln in lines if _YAML_LINE.match(ln) or _YAML_ITEM.match(ln)]
    nested = any(ln[:1] in " -" for ln in lines)
    return len(keyed) >= 3 and nested and len(shaped) >= 0.9 * len(lines)


# --- HTML ---------------------------------------------------------------------------------


class _Html(HTMLParser):
    def __init__(self, raw: str) -> None:
        super().__init__(convert_charrefs=False)
        self.raw = raw
        self.line_starts = [0]
        for m in re.finditer("\n", raw):
            self.line_starts.append(m.end())
        self.stack: list[tuple[str, dict[str, str]]] = []
        self.run: list[tuple[int, int]] = []
        self.out: list[Segment] = []
        self.section = ""
        self.row: list[tuple[str, int, int]] | None = None
        self.row_start = 0
        self.cell: list[tuple[int, int]] | None = None
        self.skipping: list[str] = []  # the open tags whose text is not visible
        self.data_tag: tuple[str, int] | None = None

    def _unskip(self, tag: str) -> None:
        if tag in self.skipping:
            del self.skipping[len(self.skipping) - 1 - self.skipping[::-1].index(tag) :]

    def at(self) -> int:
        line, col = self.getpos()
        return self.line_starts[line - 1] + col

    def _hidden(self) -> bool:
        for _, a in self.stack:
            style = a.get("style", "").replace(" ", "").lower()
            if "display:none" in style or "visibility:hidden" in style:
                return True
            if "hidden" in a or a.get("aria-hidden") == "true":
                return True
        return False

    def _inside(self, *tags: str) -> str | None:
        return next((t for t, _ in reversed(self.stack) if t in tags), None)

    def _classes(self) -> str:
        return " ".join(a.get("class", "") for _, a in self.stack if a.get("class")).strip()

    def flush(self) -> None:
        if not self.run:
            return
        start, end = self.run[0][0], self.run[-1][1]
        self.run = []
        text = visible(self.raw[start:end])
        if not text:
            return
        if self.cell is not None:
            self.cell.append((start, end))
            return
        heading = self._inside("h1", "h2", "h3", "h4", "h5", "h6")
        attrs = {"class": self._classes()} if self._classes() else {}
        owner = next((a["id"] for _, a in reversed(self.stack) if a.get("id")), "")
        if owner:
            attrs["id"] = owner  # the nearest element with an ID: the post or card it is in
        if self._inside("blockquote", "q") is not None:
            attrs["quoted"] = "true"  # words quoted from elsewhere, not the block's own
        if self._hidden():
            kind = "hidden"
        elif self._inside("footer"):
            kind = "footer"
        elif heading is not None:
            level = int(heading[1])
            if level >= 2:
                self.section = text
            self.out.append(Segment("heading", start, end, text, self.section, level))
            return
        elif self._inside("li", "dd", "dt"):
            kind = "item"
        else:
            kind = "paragraph"
        self.out.append(Segment(kind, start, end, text, self.section, attrs=attrs))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        pos = self.at()
        text = self.get_starttag_text() or ""
        if tag == "meta" or (tag == "link" and "canonical" in a.get("rel", "")):
            self.out.append(Segment("meta", pos, pos + len(text), text, attrs=a))
            return
        if tag in _SKIP:
            if tag == "script" and "ld+json" in a.get("type", ""):
                self.data_tag = (tag, pos + len(text))
            if tag != "head":  # the head's title and meta tags are read
                self.skipping.append(tag)
            return
        if tag == "title":
            self.data_tag = ("title", pos + len(text))
            self.skipping.append(tag)
            return
        if tag in BLOCK_TAGS:
            self.flush()
        if tag == "tr":
            self.row, self.row_start = [], pos
        if tag in ("td", "th") and self.row is not None:
            self.cell = []
        if tag not in _VOID:
            self.stack.append((tag, a))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID and self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        pos = self.at()
        if self.data_tag is not None and tag == self.data_tag[0]:
            start = self.data_tag[1]
            body = self.raw[start:pos]
            lead = len(body) - len(body.lstrip())
            s, e = start + lead, start + len(body.rstrip())
            if e > s:
                kind = "title" if tag == "title" else "data"
                self.out.append(Segment(kind, s, e, visible(self.raw[s:e]) if kind == "title"
                                        else self.raw[s:e]))  # fmt: skip
            self.data_tag = None
            self._unskip(tag)
            return
        if tag in _SKIP or tag == "title":
            self._unskip(tag)
            return
        if tag in BLOCK_TAGS:
            self.flush()
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            cell = self.cell
            self.cell = None
            if cell:
                s, e = cell[0][0], cell[-1][1]
                text = " ".join(visible(self.raw[a:b]) for a, b in cell)  # no hidden text
                self.row.append((text, s, e))
            else:
                self.row.append(("", pos, pos))
        if tag == "tr" and self.row is not None:
            cells = tuple(self.row)
            self.row = None
            filled = [c for c in cells if c[0]]
            if filled:
                s, e = filled[0][1], filled[-1][2]
                text = " | ".join(c[0] for c in cells)
                kind = "header" if self._header_row() else "row"
                self.out.append(Segment(kind, s, e, text, self.section, cells=cells))
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def _header_row(self) -> bool:
        return bool(self._th_only)

    _th_only = False

    def handle_data(self, data: str) -> None:
        if self.skipping or not data.strip():
            return
        pos = self.at()
        lead = len(data) - len(data.lstrip())
        span = (pos + lead, pos + len(data.rstrip()))
        if self.cell is not None and self._hidden():
            # Hidden text inside a cell is its own hidden segment, never part of the row.
            self.flush()
            self.out.append(Segment("hidden", *span, visible(self.raw[span[0] : span[1]]),
                                    self.section))  # fmt: skip
            return
        self.run.append(span)

    def handle_entityref(self, name: str) -> None:
        if not self.skipping:
            pos = self.at()
            self.run.append((pos, pos + len(name) + 2))

    def handle_charref(self, name: str) -> None:
        if not self.skipping:
            pos = self.at()
            self.run.append((pos, pos + len(name) + 3))


def _html(raw: str) -> list[Segment]:
    parser = _Html(raw)
    # A header row is one of only header cells: a row led by a row-header cell (``<th
    # scope="row">``) and holding data cells is a data row.
    th_rows = {
        m.start()
        for m in re.finditer(r"<tr\b[^>]*>(.*?)(?=<tr\b|</table|$)", raw, re.I | re.S)
        if re.match(r"\s*<th\b", m.group(1), re.I) and not re.search(r"<td\b", m.group(1), re.I)
    }
    parser.feed(raw)
    parser.close()
    parser.flush()
    out = []
    for s in parser.out:
        if s.kind in ("row", "header"):
            opened = raw.rfind("<tr", 0, s.start)
            kind = "header" if opened in th_rows else "row"
            s = Segment(kind, s.start, s.end, s.text, s.section, cells=s.cells)
        out.append(s)
    return out


# --- text, Markdown, email ----------------------------------------------------------------

_ITEM = re.compile(r"^([-*+•▪◦]|\d{1,3}[.)]|[A-Z]{1,2}\d{1,2}[.)]|[a-z][.)]|\(\w{1,3}\))\s+")
_HEADING_MD = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BOLD_LINE = re.compile(r"^(\*\*|__)([^*_]{2,80}?)(\1)\s*:?\s*$")
_SLIDE = re.compile(r"^(slide|page)\s+\d+\s*[:.\-\u2013\u2014]\s*(.+)$", re.I)
_COLON_HEAD = re.compile(r"^([A-Z][^.!?:]{1,60}):\s*$")
_META_LINE = re.compile(r"^([A-Za-z][A-Za-z _-]{0,30}):[ \t]*(.*?)[ \t]*$")
_RULE = re.compile(r"^(?:[-=_*~]{3,})\s*(?:((?:page|slide)\s+(\d+))\s*[-=_*~]{3,})?\s*$", re.I)
"""A thematic break (a rule line), or a page separator such as ``--- page 3 ---``."""
_TURN = re.compile(r"^\[?(\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)\]?\s+([^:\n\[\]]{1,40}?):\s+(?=\S)")
"""A speaker turn of a call transcript: ``[00:01:20] Speaker 1: ...``."""
_LABEL_LINE = re.compile(r"^\[[A-Za-z][^\[\]]{0,38}\]$")
"""A line that is only a bracketed label (``[speaker notes]``)."""


def front_matter_end(raw: str) -> int:
    """Where the body starts after ``---`` front matter (0 without one)."""
    if not raw.startswith("---\n"):
        return 0
    end = raw.find("\n---", 4)
    if end < 0:
        return 0
    nl = raw.find("\n", end + 4)
    return len(raw) if nl < 0 else nl + 1


def _lines(raw: str, start: int, end: int) -> Iterator[tuple[int, int]]:
    pos = start
    for line in raw[start:end].splitlines(keepends=True):
        yield pos, pos + len(line.rstrip("\r\n"))
        pos += len(line)


def _meta_lines(raw: str, start: int, end: int) -> list[Segment]:
    out = []
    for s, e in _lines(raw, start, end):
        m = _META_LINE.match(raw[s:e])
        if m is None or not m.group(2):
            continue
        vs, ve = s + m.start(2), s + m.end(2)
        value = raw[vs:ve]
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            vs, ve = vs + 1, ve - 1
        attrs = {"key": m.group(1).strip().lower(), "value": raw[vs:ve],
                 "value_start": str(vs), "value_end": str(ve)}  # fmt: skip
        out.append(Segment("meta", s, e, raw[s:e], attrs=attrs))
    return out


def _pipe_cells(raw: str, s: int, e: int) -> tuple[tuple[str, int, int], ...]:
    line = raw[s:e]
    bars = [i for i, ch in enumerate(line) if ch == "|"]
    if line.strip() and not line.lstrip().startswith("|"):
        bars = [-1, *bars]
    if not line.rstrip().endswith("|"):
        bars.append(len(line))
    out = []
    for a, b in itertools.pairwise(bars):
        cs, ce = s + a + 1, s + b
        while cs < ce and raw[cs].isspace():
            cs += 1
        while ce > cs and raw[ce - 1].isspace():
            ce -= 1
        out.append((raw[cs:ce].strip("*_ "), cs, ce))
    return tuple(out)


def _text(raw: str, fmt: str) -> list[Segment]:
    out: list[Segment] = []
    body = 0
    if fmt == "email":
        head_end = raw.find("\n\n")
        head_end = len(raw) if head_end < 0 else head_end
        out.extend(_meta_lines(raw, 0, head_end))
        body = head_end
    else:
        body = front_matter_end(raw)
        if body:
            out.extend(_meta_lines(raw, 4, body))
    section = ""
    para: list[tuple[int, int]] = []
    open_item = False
    page_title = False  # the first short line after a page separator is the page's title

    def flush() -> None:
        if not para:
            return
        s, e = para[0][0], para[-1][1]
        para.clear()
        out.append(Segment("paragraph", s, e, " ".join(raw[s:e].split()), section))

    for s, e in _lines(raw, body, len(raw)):
        line = raw[s:e]
        text = line.strip()
        if not text:
            flush()
            open_item = False
            continue
        lead = len(line) - len(line.lstrip())
        s0 = s + lead
        if text.startswith(">"):
            text = text.lstrip("> ").strip()
            if not text:
                flush()
                continue
            s0 = s + line.index(text[0]) if text else s0
        rule = _RULE.match(text)
        if rule:
            if len(para) == 1 and text[:1] in "=-" and not rule.group(1):
                ps, pe = para.pop()  # a setext underline: the line above is a heading
                level = 1 if text[:1] == "=" else 2
                if level >= 2:
                    section = raw[ps:pe]
                out.append(Segment("heading", ps, pe, raw[ps:pe], section, level))
                continue
            flush()
            open_item = False
            attrs = {"page": rule.group(2)} if rule.group(2) else {}
            out.append(Segment("rule", s0, s0 + len(text), "", section, attrs=attrs))
            page_title = bool(rule.group(2))
            continue
        if page_title and not para:
            page_title = False
            if len(text) <= 60 and text[-1:] not in ".!?" and not _ITEM.match(text) \
                    and "|" not in text:  # fmt: skip
                section = text
                out.append(Segment("heading", s0, s0 + len(text), text, section, 2))
                continue
        if _LABEL_LINE.match(text):  # "[speaker notes]": a label that starts a part of the page
            flush()
            open_item = False
            out.append(Segment("heading", s0, s0 + len(text), text.strip("[] "), section, 3))
            continue
        turn = _TURN.match(text)
        if turn:
            flush()
            open_item = False
            ts = s0 + turn.end()
            out.append(Segment("paragraph", ts, s0 + len(text), raw[ts : s0 + len(text)], section,
                               attrs={"speaker": turn.group(2).strip(),
                                      "time": turn.group(1)}))  # fmt: skip
            continue
        m = _HEADING_MD.match(text)
        b = _BOLD_LINE.match(text)
        sl = _SLIDE.match(text)
        c = _COLON_HEAD.match(text) if not para else None
        if m or b or sl or c:
            flush()
            open_item = False
            if m:
                level = len(m.group(1))
                hs = s0 + text.index(m.group(2)) if m.group(2) else s0
                he = hs + len(m.group(2))
            elif b:
                level = 3
                hs = s0 + len(b.group(1))
                he = hs + len(b.group(2).rstrip())
            elif sl:
                level = 2
                hs = s0 + text.index(sl.group(2))
                he = hs + len(sl.group(2).rstrip())
            else:
                assert c is not None
                level = 3
                hs, he = s0, s0 + len(c.group(1))
            heading = raw[hs:he]
            if level >= 2:
                section = heading
            out.append(Segment("heading", hs, he, heading, section, level))
            continue
        if text.startswith("|") or (text.count("|") >= 2 and fmt == "markdown"):
            flush()
            open_item = False
            cells = _pipe_cells(raw, s0, s0 + len(text))
            if all(re.fullmatch(r":?-{2,}:?", c[0]) for c in cells if c[0]):
                if out and out[-1].kind == "row":
                    prev = out[-1]
                    out[-1] = Segment("header", prev.start, prev.end, prev.text, prev.section,
                                      cells=prev.cells)  # fmt: skip
                continue
            filled = [c for c in cells if c[0]]
            if filled:
                out.append(Segment("row", s0, s0 + len(text), " | ".join(c[0] for c in cells),
                                   section, cells=cells))  # fmt: skip
            continue
        im = _ITEM.match(text)
        if im:
            flush()
            ts = s0 + im.end()
            out.append(Segment("item", ts, s0 + len(text), raw[ts : s0 + len(text)], section,
                               attrs={"marker": im.group(1)}))  # fmt: skip
            open_item = True
            continue
        if not para and open_item and out and out[-1].kind == "item":
            prev = out.pop()  # a hard-wrapped item: the line goes on the item above it
            out.append(Segment("item", prev.start, s0 + len(text),
                               " ".join(raw[prev.start : s0 + len(text)].split()), section,
                               attrs=prev.attrs))  # fmt: skip
            continue
        para.append((s0, s0 + len(text)))
    flush()
    return out


def segment(raw: str, fmt: str) -> tuple[Segment, ...]:
    """``raw`` cut into segments by structure (module doc, item 2). Tables are rows."""
    if fmt == "html":
        return tuple(_html(raw))
    if fmt == "vtt":
        return tuple(_vtt(raw))
    if fmt == "xml":
        return tuple(_xml(raw))
    if fmt == "yaml":
        return tuple(_yaml_segments(raw))
    if fmt == "jsonl":
        rows = read_rows(raw, fmt).rows
        return tuple(
            Segment("row", r.start, r.end, " | ".join(v for v, _, _ in r.cells.values()),
                    cells=tuple(r.cells.values()))
            for r in rows
        )  # fmt: skip
    if fmt in ("csv", "json"):
        table = read_rows(raw, fmt)
        head: list[Segment] = []
        if fmt == "json":
            for key, (value, vs, ve) in json_header_cells(raw):
                if ve <= vs or raw[vs - 1 : vs] != '"':
                    continue  # strings only: a number or a flag says nothing a reader quotes
                ks = raw.rfind('"' + key.split(".", 1)[0] + '"', 0, vs)  # from the outer key
                head.append(Segment("meta", ks if ks >= 0 else vs, ve + 1, raw[vs:ve],
                                    attrs={"key": key, "value": value, "value_start": str(vs),
                                           "value_end": str(ve)}))  # fmt: skip
        if fmt == "csv" and table.columns:
            found = list(delimited_cells(raw, table.delimiter or ","))
            first = found[header_index(found)] if found else []
            if first:
                head.append(
                    Segment(
                        "header",
                        first[0][1],
                        first[-1][2],
                        " | ".join(v for v, _, _ in first),
                        cells=tuple(first),
                    )
                )
        return (
            *head,
            *(
                Segment(
                    "row",
                    r.start,
                    r.end,
                    " | ".join(v for v, _, _ in r.cells.values()),
                    cells=tuple(r.cells.values()),
                )
                for r in table.rows
            ),
        )
    return tuple(_text(raw, fmt))


# --- transcripts, XML and YAML --------------------------------------------------------------

_CUE_TIME = re.compile(r"^\s*(\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}\s+-->\s+")
_VOICE = re.compile(r"<v(?:\.[\w.]+)?\s+([^>]+)>")


def _vtt(raw: str) -> list[Segment]:
    """A WebVTT transcript: each cue a ``paragraph`` with its ``speaker`` and ``time``.

    A cue's range is its spoken text alone (the voice tag left out), so a quote of what a
    speaker said cites only their words. ``NOTE`` blocks are read as text: a ``key: value``
    line is ``meta``, any other line a ``paragraph``.
    """
    out: list[Segment] = []
    blocks: list[list[tuple[int, int]]] = [[]]
    for s, e in _lines(raw, 0, len(raw)):
        if raw[s:e].strip():
            blocks[-1].append((s, e))
        elif blocks[-1]:
            blocks.append([])
    for block in blocks:
        if not block:
            continue
        first = raw[block[0][0] : block[0][1]].strip()
        if first.startswith("WEBVTT"):
            continue
        if first.startswith("NOTE"):
            lines = block[1:] if first == "NOTE" else [(block[0][0] + 4, block[0][1]), *block[1:]]
            for ls, le in lines:
                while ls < le and raw[ls].isspace():
                    ls += 1
                metas = _meta_lines(raw, ls, le)
                out.extend(metas or [Segment("paragraph", ls, le, " ".join(raw[ls:le].split()))])
            continue
        at = next((i for i, (ls, le) in enumerate(block) if _CUE_TIME.match(raw[ls:le])), None)
        if at is None:
            continue
        time = raw[block[at][0] : block[at][1]].split("-->")[0].strip()
        payload = block[at + 1 :]
        if not payload:
            continue
        ps, pe = payload[0][0], payload[-1][1]
        speaker = ""
        voice = _VOICE.match(raw, ps)
        if voice:
            speaker, ps = voice.group(1).strip(), voice.end()
        text = re.sub(r"</?[^>]+>", "", raw[ps:pe])
        out.append(Segment("paragraph", ps, pe, " ".join(text.split()),
                           attrs={"speaker": speaker, "time": time}))  # fmt: skip
    return out


_XML_TAG = re.compile(
    r"<(/?)([A-Za-z_][\w.:-]*)((?:\s+[\w.:-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*)\s*(/?)>"
)
_XML_ATTR = re.compile(r"([\w.:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")


def _xml(raw: str) -> list[Segment]:
    """An XML document: each element that holds text, and each element with attributes.

    An element holding only text is ``meta`` (``attrs["key"]`` its tag, each attribute as
    ``@name``), or an ``item`` when its parent holds several of the same tag (a list of
    steps); its range is its text alone. An element holding other elements is a
    ``heading`` at its depth, its text its attributes, so a record of repeated elements
    is cut into sections by them (``record_sections``). Comments and declarations are
    left out.
    """
    clean = re.sub(r"<!--.*?-->|<\?.*?\?>|<!\[CDATA\[|\]\]>", lambda m: " " * len(m.group(0)),
                   raw, flags=re.S)  # fmt: skip
    stack: list[tuple[str, dict[str, str], int, int, list[str]]] = []
    out: list[tuple[int, Segment]] = []
    for m in _XML_TAG.finditer(clean):
        closing, tag, attr_text, empty = m.group(1), m.group(2), m.group(3), m.group(4)
        attrs = {"@" + a.group(1): a.group(2) if a.group(2) is not None else a.group(3) or ""
                 for a in _XML_ATTR.finditer(attr_text)}  # fmt: skip
        if not closing:
            if stack:
                stack[-1][4].append(tag)
            if empty:
                if attrs:
                    text = " ".join(f"{k[1:]}={v}" for k, v in attrs.items())
                    at = stack[-1][2] if stack else -1
                    seg = Segment("meta", m.start(), m.end(), text, level=len(stack),
                                  attrs={"key": tag, **attrs})  # fmt: skip
                    out.append((at, seg))
                continue
            stack.append((tag, attrs, m.start(), m.end(), []))
            continue
        while stack and stack[-1][0] != tag:
            stack.pop()
        if not stack:
            continue
        name, own, open_at, body_at, children = stack.pop()
        depth = len(stack)
        parent = stack[-1][2] if stack else -1
        if children:
            text = " ".join(f"{k[1:]}={v}" for k, v in own.items()) or name
            out.append((parent, Segment("heading", open_at, body_at, text, level=depth + 1,
                                         attrs={"tag": name, **own})))  # fmt: skip
            continue
        bs, be = body_at, m.start()
        while bs < be and clean[bs].isspace():
            bs += 1
        while be > bs and clean[be - 1].isspace():
            be -= 1
        if be <= bs and not own:
            continue
        out.append((parent, Segment("meta", bs, be, " ".join(raw[bs:be].split()), level=depth + 1,
                                     attrs={"key": name, **own, "value": raw[bs:be]})))  # fmt: skip
    # A leaf is an item when its parent holds several of its tag: count them now, at the end.
    counts: dict[tuple[int, str], int] = {}
    for parent, seg in out:
        if seg.kind == "meta":
            k = (parent, seg.attrs.get("key", ""))
            counts[k] = counts.get(k, 0) + 1
    fixed = []
    for parent, seg in sorted(out, key=lambda x: x[1].start):
        if seg.kind == "meta" and counts.get((parent, seg.attrs.get("key", "")), 0) > 1:
            seg = Segment("item", seg.start, seg.end, seg.text, seg.section, seg.level,
                          attrs=seg.attrs)  # fmt: skip
        fixed.append(seg)
    return fixed


def _yaml_segments(raw: str) -> list[Segment]:
    """A YAML document line by line: ``key: value`` is ``meta``, ``- key: value`` an ``item``.

    ``attrs["key"]`` is the key, ``attrs["value"]`` its value (quotes left out) and the
    value's own range; a key opening a block (``credited:``) is a ``heading`` at its
    indentation, a ``- value`` item a plain ``item``. Comments are left out.
    """
    out: list[Segment] = []
    for s, e in _lines(raw, 0, len(raw)):
        line = raw[s:e]
        if not line.strip() or line.lstrip().startswith("#") or line.strip() == "---":
            continue
        m = _YAML_LINE.match(line)
        indent = len(line) - len(line.lstrip(" -"))
        if m:
            key = m.group(2).strip()
            if m.group(3) is None or m.group(3) == "":
                out.append(Segment("heading", s + m.start(2), s + m.end(2), key,
                                   level=indent // 2 + 1, attrs={"key": key}))  # fmt: skip
                continue
            vs, ve = s + m.start(3), s + m.end(3)
            if ve - vs >= 2 and raw[vs] == raw[ve - 1] and raw[vs] in "'\"":
                vs, ve = vs + 1, ve - 1
            kind = "item" if line.lstrip().startswith("-") else "meta"
            out.append(Segment(kind, s + len(line) - len(line.lstrip()), e, raw[vs:ve],
                               level=indent // 2 + 1,
                               attrs={"key": key, "value": raw[vs:ve], "value_start": str(vs),
                                      "value_end": str(ve)}))  # fmt: skip
            continue
        item = _YAML_ITEM.match(line)
        if item:
            vs, ve = s + item.start(2), s + item.end(2)
            out.append(Segment("item", vs, ve, raw[vs:ve], level=indent // 2 + 1,
                               attrs={"marker": "-"}))  # fmt: skip
        else:
            ls = s + len(line) - len(line.lstrip())
            out.append(Segment("paragraph", ls, e, " ".join(line.split())))
    return out


def _yaml_tree(raw: str) -> Any:
    """A YAML document's mappings and lists, each scalar ``(text, start, end)``.

    The block subset exports use: ``key: value``, ``key:`` opening a nested block, ``- ``
    list items (scalars or mappings), comments and quoted scalars. Flow collections and
    multi-line scalars are read as text.
    """
    lines = []
    for s, e in _lines(raw, 0, len(raw)):
        line = raw[s:e]
        if line.strip() and not line.lstrip().startswith("#") and line.strip() != "---":
            lines.append((s, e))

    def scalar(vs: int, ve: int) -> Any:
        if ve - vs >= 2 and raw[vs] == "{" and raw[ve - 1] == "}":
            return flow(vs + 1, ve - 1)
        if ve - vs >= 2 and raw[vs] == raw[ve - 1] and raw[vs] in "'\"":
            vs, ve = vs + 1, ve - 1
        return raw[vs:ve], vs, ve

    def flow(vs: int, ve: int) -> dict[str, Any]:
        """A flow mapping's ``key: value`` pairs (``{idea: X-1, what: "..."}``)."""
        out: dict[str, Any] = {}
        i = vs
        while i < ve:
            while i < ve and raw[i] in " ,":
                i += 1
            colon = raw.find(":", i, ve)
            if colon < 0:
                break
            key = raw[i:colon].strip()
            j = colon + 1
            while j < ve and raw[j] == " ":
                j += 1
            if j < ve and raw[j] in "'\"":
                end = raw.find(raw[j], j + 1, ve)
                end = ve if end < 0 else end + 1
            else:
                end = raw.find(",", j, ve)
                end = ve if end < 0 else end
            e = end
            while e > j and raw[e - 1] == " ":
                e -= 1
            out[key] = scalar(j, e)
            i = end
        return out

    def block(i: int, indent: int) -> tuple[Any, int]:
        if i >= len(lines):
            return {}, i
        s, e = lines[i]
        first = raw[s:e]
        lead = len(first) - len(first.lstrip())
        if first.lstrip().startswith("- ") or first.strip() == "-":
            items: list[Any] = []
            while i < len(lines):
                s, e = lines[i]
                line = raw[s:e]
                lead = len(line) - len(line.lstrip())
                if lead != indent or not line.lstrip().startswith("-"):
                    break
                body = s + lead + 1
                while body < e and raw[body] == " ":
                    body += 1
                if _YAML_LINE.match(" " * (body - s) + raw[body:e]):
                    sub = body - s  # a mapping item: its first key sits after the dash
                    lines[i] = (body, e)
                    value, i = mapping(i, sub, s0=body)
                    items.append(value)
                else:
                    items.append(scalar(body, e))
                    i += 1
            return items, i
        return mapping(i, lead)

    def mapping(i: int, indent: int, s0: int | None = None) -> tuple[Any, int]:
        out: dict[str, Any] = {}
        while i < len(lines):
            s, e = lines[i]
            line = raw[s:e]
            lead = (s0 - s if s0 is not None and s == s0 else 0) + len(line) - len(line.lstrip())
            if s0 is not None and s == s0:
                lead = indent
            if lead != indent or line.lstrip().startswith("- "):
                break
            m = _YAML_LINE.match(line)
            if m is None:
                break
            key = m.group(2).strip()
            if m.group(3):
                out[key] = scalar(s + m.start(3), s + m.end(3))
                i += 1
                continue
            i += 1
            if i < len(lines):
                ns, ne = lines[i]
                nxt = raw[ns:ne]
                nlead = len(nxt) - len(nxt.lstrip())
                if nlead > indent or (nlead == indent and nxt.lstrip().startswith("- ")):
                    out[key], i = block(i, nlead)
                    continue
            out[key] = ("", e, e)
        return out, i

    tree, _ = block(0, 0)
    return tree


# --- tables -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One table row: each column's ``(value, start, end)`` and the row's raw range."""

    n: int
    start: int
    end: int
    cells: Mapping[str, tuple[str, int, int]]

    def value(self, column: str) -> str:
        """The row's value in ``column`` ("" when it has none)."""
        found = self.cells.get(column)
        return "" if found is None else found[0]


@dataclass(frozen=True)
class Rows:
    """A table read from a record: its columns in order, its rows, its scalar header."""

    columns: tuple[str, ...]
    rows: tuple[Row, ...]
    header: Mapping[str, Any] = field(default_factory=dict)
    delimiter: str = ""


def delimited_cells(raw: str, delimiter: str) -> Iterator[list[tuple[str, int, int]]]:
    """RFC 4180 rows of ``raw`` with ``delimiter``: each cell's value and raw range."""
    i, n = 0, len(raw)
    row: list[tuple[str, int, int]] = []
    while i <= n:
        if i < n and raw[i] == '"':
            j, buf = i + 1, []
            while j < n:
                if raw[j] == '"' and j + 1 < n and raw[j + 1] == '"':
                    buf.append('"')
                    j += 2
                elif raw[j] == '"':
                    break
                else:
                    buf.append(raw[j])
                    j += 1
            row.append(("".join(buf), i + 1, j))
            i = j + 1
            while i < n and raw[i] not in delimiter + "\r\n":
                i += 1
        else:
            j = i
            while j < n and raw[j] not in delimiter + "\r\n":
                j += 1
            s, e = i, j
            while s < e and raw[s] in " \t" and delimiter != "\t":
                s += 1
            while e > s and raw[e - 1] in " \t" and delimiter != "\t":
                e -= 1
            row.append((raw[s:e], s, e))
            i = j
        if i >= n or raw[i] in "\r\n":
            if any(v for v, _, _ in row):
                yield row
            row = []
            if i < n and raw[i] == "\r":
                i += 1
            i += 1
        else:
            i += 1


_JSON_ATOM = re.compile(r"-?\d+(\.\d+)?([eE][-+]?\d+)?|true|false|null")


def _ws(raw: str, i: int) -> int:
    while i < len(raw) and raw[i] in " \t\r\n":
        i += 1
    return i


def _json_string(raw: str, i: int) -> tuple[tuple[str, int, int], int]:
    """The string starting at the quote ``raw[i]``: its value and raw range, and the end."""
    start = i + 1
    j = start
    while raw[j] != '"':
        j += 2 if raw[j] == "\\" else 1
    return (json.loads(raw[start - 1 : j + 1]), start, j), j + 1


def _json_value(raw: str, i: int) -> tuple[Any, int]:
    """A JSON value at ``i`` with every scalar as ``(text, start, end)``, and where it ends.

    A position-tracking scanner: each string value keeps its raw range, so a row read
    from a JSON export cites the exact characters of its fields.
    """
    i = _ws(raw, i)
    ch = raw[i]
    if ch == "{":
        out: dict[str, Any] = {}
        i = _ws(raw, i + 1)
        if raw[i] == "}":
            return out, i + 1
        while True:
            (key, _, _), i = _json_string(raw, _ws(raw, i))
            i = _ws(raw, i) + 1  # the colon
            out[key], i = _json_value(raw, i)
            i = _ws(raw, i)
            if raw[i] == ",":
                i += 1
                continue
            return out, i + 1
    if ch == "[":
        items: list[Any] = []
        i = _ws(raw, i + 1)
        if raw[i] == "]":
            return items, i + 1
        while True:
            item, i = _json_value(raw, i)
            items.append(item)
            i = _ws(raw, i)
            if raw[i] == ",":
                i += 1
                continue
            return items, i + 1
    if ch == '"':
        return _json_string(raw, i)
    m = _JSON_ATOM.match(raw, i)
    if m is None:
        raise ValueError(f"not JSON at {i}")
    return (m.group(0), m.start(), m.end()), m.end()


def _flatten(obj: Mapping[str, Any], prefix: str = "") -> dict[str, tuple[str, int, int]]:
    out: dict[str, tuple[str, int, int]] = {}
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        elif isinstance(v, tuple):
            out[key] = v
        elif isinstance(v, list) and v and all(isinstance(x, tuple) for x in v):
            s, e = v[0][1], v[-1][2]
            out[key] = (", ".join(x[0] for x in v), s, e)
    return out


def _lists(value: Any) -> Iterator[list[dict[str, Any]]]:
    if isinstance(value, list):
        objs = [v for v in value if isinstance(v, dict)]
        if objs and len(objs) == len(value):
            yield objs
        for v in value:
            yield from _lists(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _lists(v)


def _records(obj: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """``obj``'s repeated records: ``(record, parent fields)`` pairs, deepest list first.

    An export that groups its records under a parent (comments under each post, lines
    under each order) holds them in a list of objects inside each object. The records are
    the inner items, each carrying its parent's other fields; an object holding no such
    list is its own record.
    """
    for k, v in obj.items():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            parent = {pk: pv for pk, pv in obj.items() if pk != k}
            out = []
            for item in v:
                for rec, above in _records(item):
                    out.append((rec, {**parent, **above}))
            return out
    return [(obj, {})]


def _json_rows(raw: str) -> Rows:
    data, _ = _json_value(raw, 0)
    return _tree_rows(data, "json")


def _tree_rows(data: Any, delimiter: str) -> Rows:
    """The records of a parsed tree (JSON, JSON Lines, YAML): its largest list of objects."""
    lists = list(_lists(data))
    if not lists:
        return Rows((), ())
    expanded = [[r for obj in objs for r in _records(obj)] for objs in lists]
    best = max(expanded, key=len)
    rows = []
    for n, (obj, parent) in enumerate(best):
        own = _flatten(obj)
        if not own:
            continue
        s = min(c[1] for c in own.values())
        e = max(c[2] for c in own.values())
        cells = {**own, **{k: v for k, v in _flatten(parent).items() if k not in own}}
        rows.append(Row(n, s, e, cells))
    columns = tuple(dict.fromkeys(k for r in rows for k in r.cells))
    header: dict[str, Any] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, tuple) and not k.startswith("_"):
                header[k] = v[0]
            elif isinstance(v, dict) and not k.startswith("_"):
                for sub, cell in _flatten(v, k + ".").items():
                    header[sub] = cell[0]
    return Rows(columns, tuple(rows), header, delimiter)


def json_header_cells(raw: str) -> list[tuple[str, tuple[str, int, int]]]:
    """A JSON object's top-level fields outside its records: each key and its value's range.

    A feed names its publisher there ("publisher", "title", "published"); nested objects
    are flattened (``publisher.name``). Keys starting ``_`` are comments and are left out.
    """
    try:
        data, _ = _json_value(raw, 0)
    except (IndexError, KeyError, ValueError):
        return []
    out: list[tuple[str, tuple[str, int, int]]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k.startswith("_"):
                continue
            if isinstance(v, tuple):
                out.append((k, v))
            elif isinstance(v, dict):
                out.extend(_flatten(v, k + ".").items())
    return out


def header_index(rows: Sequence[Sequence[tuple[str, int, int]]]) -> int:
    """Which delimited row is the header: the first that fills most of the table's width.

    A sheet pasted with a title line above its header ("Shortlist, pasted from the ops
    sheet") starts with a row of one filled cell; the header is the first row that
    fills at least half as many cells as the fullest row, and never a single cell.
    """
    filled = [sum(1 for v, _, _ in r if v.strip()) for r in rows]
    widest = max(filled, default=0)
    for i, n in enumerate(filled):
        if n >= max(2, (widest + 1) // 2):
            return i
    return 0


def read_rows(raw: str, fmt: str = "csv") -> Rows:
    """The table in ``raw`` (module doc, item 3); raises ``ValueError`` on broken JSON."""
    if fmt == "json":
        try:
            return _json_rows(raw)
        except (IndexError, KeyError) as e:
            raise ValueError("the JSON does not parse") from e
    if fmt == "jsonl":
        try:
            data = [_json_value(raw, m.start())[0] for m in re.finditer(r"(?m)^[ \t]*\{", raw)]
        except (IndexError, KeyError) as e:
            raise ValueError("a JSON line does not parse") from e
        return _tree_rows(data, "jsonl")
    if fmt == "yaml":
        return _tree_rows(_yaml_tree(raw), "yaml")
    delimiter = _delimiter(raw.splitlines()) or ","
    all_rows = list(delimited_cells(raw, delimiter))
    if not all_rows:
        return Rows((), (), {}, delimiter)
    at = header_index(all_rows)
    columns = tuple(v.strip() for v, _, _ in all_rows[at])
    rows = []
    for n, cells in enumerate(all_rows[at + 1 :]):
        named = {c: cells[i] for i, c in enumerate(columns) if i < len(cells)}
        filled = [c for c in cells if c[2] > c[1]] or cells
        start, end = filled[0][1], filled[-1][2]
        if cells[0][2] <= cells[0][1]:
            start = cells[0][1]  # a blank first cell (a value filled down) stays in the row
        if start > 0 and raw[start - 1] == '"':
            start -= 1  # a quoted first cell: the row starts at its opening quote
        if end < len(raw) and raw[end] == '"':
            end += 1
        rows.append(Row(n, start, end, named))
    return Rows(columns, tuple(rows), {}, delimiter)


# --- dates --------------------------------------------------------------------------------

MONTHS = {
    m: i
    for i, names in enumerate(
        (("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
         ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
         ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
         ("dec", "december")),
        start=1,
    )
    for m in names
}  # fmt: skip
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"  # noqa: E501
DATE_PATTERN = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MON}\.?,?\s+\d{{4}}"
    rf"|{_MON}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    r"|\d{1,2}[./]\d{1,2}[./]\d{4}|\d{4}/\d{1,2}/\d{1,2})\b",
    re.I,
)
"""A date as people write it, anywhere in a text."""


def _ymd(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def parse_date(text: str, order: str = "") -> str | None:
    """``text`` (one date, module doc item 4) as ISO; None when it is not a date.

    ``order`` (``dmy`` or ``mdy``) decides an all-numeric date valid both ways; without
    it such a date stays ambiguous and is None.
    """
    t = " ".join(text.strip().split()).rstrip(".,")
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t, flags=re.I)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", t)
    if m:
        return _ymd(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})", t)
    if m:
        return _ymd(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(rf"(\d{{1,2}})\s+{_MON}\.?,?\s+(\d{{4}})", t, re.I)
    if m:
        return _ymd(int(m[3]), MONTHS[m[2].lower()[:3]], int(m[1]))
    m = re.fullmatch(rf"{_MON}\.?\s+(\d{{1,2}}),?\s+(\d{{4}})", t, re.I)
    if m:
        return _ymd(int(m[3]), MONTHS[m[1].lower()[:3]], int(m[2]))
    m = re.fullmatch(r"(\d{1,2})([./])(\d{1,2})\2(\d{4})", t)
    if m:
        a, b, y = int(m[1]), int(m[3]), int(m[4])
        if m[2] == "." or (a > 12 >= b) or (a == b) or order == "dmy":
            return _ymd(y, b, a)
        if (b > 12 >= a) or order == "mdy":
            return _ymd(y, a, b)
        return None
    return None


def find_dates(text: str, order: str = "") -> list[tuple[str, int, int]]:
    """Every date in ``text`` that :func:`parse_date` reads: ISO value, start, end."""
    out = []
    for m in DATE_PATTERN.finditer(text):
        iso = parse_date(m.group(0), order)
        if iso is not None:
            out.append((iso, m.start(), m.end()))
    return out
