"""The text family, standard library only: plain text, Markdown, JSON, CSV and HTML.

Each converter takes decoded text (:mod:`aksha.extensions.ingest.sniff` chose the charset)
and returns a :class:`~aksha.extensions.ingest.types.Representation`:

- **Plain text** as it is.
- **Markdown** as it is, one anchor per ATX heading, spanning to the next heading of the
  same or a higher level.
- **JSON** pretty-printed (two-space indent, keys in file order), one anchor per top-level
  key, or per item of a top-level array.
- **CSV** as it is, the delimiter sniffed among ``, ; tab |``, one anchor per block of
  rows (``rows 1-200``; the header row is row 0 and belongs to the first block).
- **HTML** through ``html.parser``: ``script``, ``style``, ``noscript`` and ``template``
  are dropped and counted, block elements end a line, each heading starts an anchor.

All of them read at the ``parse_fast`` rung with ``extracted`` trust: the text is the
file's own.
"""

from __future__ import annotations

import csv
import json
import re
from html.parser import HTMLParser

from aksha.extensions.ingest.types import Anchor, Caps, PageRung, Representation

_STDLIB = "python stdlib"
_ATX = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$", re.MULTILINE)


def plain(text: str) -> Representation:
    """Plain text, unchanged."""
    return Representation(text, (), (_STDLIB,), (PageRung("text", "parse_fast", "extracted"),))


def markdown(text: str) -> Representation:
    """Markdown, unchanged, with one anchor per heading."""
    found = [(len(m.group(1)), m.group(2).strip(), m.start()) for m in _ATX.finditer(text)]
    anchors: list[Anchor] = []
    names = Names()
    for i, (level, title, start) in enumerate(found):
        end = len(text)
        for lv, _, s in found[i + 1 :]:
            if lv <= level:
                end = s
                break
        anchors.append(Anchor(names.take(title), start, end))
    pages = (PageRung("text", "parse_fast", "extracted"),)
    return Representation(text, tuple(anchors), (_STDLIB,), pages, notes=("headings as anchors",))


def json_doc(text: str) -> Representation:
    """JSON pretty-printed with one anchor per top-level key (or array item)."""
    value = json.loads(text)
    out = json.dumps(value, indent=2, ensure_ascii=False)
    anchors: list[Anchor] = []
    names = Names()
    if isinstance(value, dict):
        at = 0
        for key in value:
            needle = "\n  " + json.dumps(key, ensure_ascii=False) + ":"
            start = out.find(needle, at)
            if start < 0:
                continue
            start += 3
            at = start
            anchors.append(Anchor(names.take(str(key)), start, start))
    elif isinstance(value, list):
        at = 0
        for i, item in enumerate(value[:10_000]):
            piece = "\n  " + json.dumps(item, indent=2, ensure_ascii=False).replace("\n", "\n  ")
            start = out.find(piece, at)
            if start < 0:
                continue
            start += 3
            at = start
            anchors.append(Anchor(f"[{i}]", start, start))
    anchors = _close(anchors, len(out))
    pages = (PageRung("text", "parse_fast", "extracted"),)
    note = "pretty-printed, top-level keys as anchors"
    return Representation(out, tuple(anchors), (_STDLIB,), pages, notes=(note,))


def csv_doc(text: str, caps: Caps | None = None) -> Representation:
    """CSV as it is, its delimiter sniffed, one anchor per block of rows."""
    caps = caps or Caps()
    sample = text[:20_000]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","
    starts = _row_starts(text)
    anchors: list[Anchor] = []
    rows = len(starts) - 1  # row 0 is the header
    per = caps.rows_per_anchor
    for first in range(1, max(rows, 0) + 1, per):
        last = min(first + per - 1, rows)
        start = 0 if first == 1 else starts[first]
        end = starts[last + 1] if last + 1 < len(starts) else len(text)
        anchors.append(Anchor(f"rows {first}-{last}", start, end))
    shown = {"\t": "tab", ",": "','", ";": "';'", "|": "'|'"}[delimiter]
    pages = (PageRung("table", "parse_fast", "extracted"),)
    return Representation(text, tuple(anchors), (_STDLIB,), pages,
                          signals=(("rows", max(rows, 0)),),
                          notes=(f"delimiter {shown}", f"{max(rows, 0)} rows"))  # fmt: skip


def _row_starts(text: str) -> list[int]:
    """The offset of each logical CSV row (a quoted field may hold line breaks)."""
    starts: list[int] = []
    pos = 0
    quotes = 0
    last = ""
    for line in text.splitlines(keepends=True):
        if quotes % 2 == 0:
            starts.append(pos)
            last = ""
        quotes += line.count('"')
        last += line
        pos += len(line)
    if starts and not last.strip():
        starts.pop()
    return starts


_DROP = frozenset({"script", "style", "noscript", "template"})
_BLOCK = frozenset({"p", "div", "li", "tr", "br", "section", "article", "header", "footer",
                    "table", "ul", "ol", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5",
                    "h6", "title", "dt", "dd", "hr", "main", "nav", "aside", "figure",
                    "figcaption", "form", "body", "head"})  # fmt: skip
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


class _Text(HTMLParser):
    """Visible text as lines; each heading's line index, so anchors land on line starts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.line: list[str] = []
        self.skip = 0
        self.dropped = 0
        self.heading: list[str] | None = None
        self.headings: list[tuple[str, int]] = []
        self.title: list[str] | None = None
        self.title_text = ""

    def flush(self) -> None:
        text = " ".join("".join(self.line).split())
        if text:
            self.lines.append(text)
        self.line = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP:
            self.skip += 1
            self.dropped += 1
            return
        if tag in _CELLS:
            if "".join(self.line).strip():
                self.line.append(" | ")
            return
        if tag in _BLOCK:
            self.flush()
        if tag in _HEADINGS and self.skip == 0:
            self.heading = []
            self.headings.append(("", len(self.lines)))
        if tag == "title":
            self.title = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP:
            self.skip = max(0, self.skip - 1)
            return
        if tag in _HEADINGS and self.heading is not None:
            _, at = self.headings[-1]
            self.headings[-1] = (" ".join("".join(self.heading).split()), at)
            self.heading = None
        if tag == "title" and self.title is not None:
            self.title_text = " ".join("".join(self.title).split())
            self.title = None
        if tag in _BLOCK:
            self.flush()

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.title is not None:
            self.title.append(data)
        if self.heading is not None:
            self.heading.append(data)
        self.line.append(data)


_CELLS = frozenset({"td", "th"})


def html_doc(text: str) -> Representation:
    """HTML as its visible text, ``script`` and ``style`` dropped, headings as anchors."""
    parser = _Text()
    parser.feed(text)
    parser.close()
    parser.flush()
    offsets: list[int] = []
    at = 0
    for line in parser.lines:
        offsets.append(at)
        at += len(line) + 1
    body = "".join(line + "\n" for line in parser.lines)
    anchors: list[Anchor] = []
    names = Names()
    for label, index in parser.headings:
        if label:
            start = offsets[index] if index < len(offsets) else len(body)
            anchors.append(Anchor(names.take(label), start, start))
    anchors = _close(anchors, len(body))
    dropped = (f"{parser.dropped} script or style blocks",) if parser.dropped else ()
    notes: tuple[str, ...] = ("visible text, headings as anchors",)
    if parser.title_text:
        notes = (f"title {parser.title_text[:80]!r}", *notes)
    pages = (PageRung("page", "parse_fast", "extracted"),)
    return Representation(body, tuple(anchors), (_STDLIB,), pages, dropped=dropped, notes=notes)


class Names:
    """Anchor labels handed out so far: ``label``, then ``label (2)``, ``label (3)``.

    Each :meth:`take` costs constant time on average, so a file with hundreds of thousands
    of identical headings stays linear (a set rebuilt per heading made it quadratic).
    """

    def __init__(self) -> None:
        """No labels yet."""
        self._used: set[str] = set()
        self._next: dict[str, int] = {}

    def take(self, label: str) -> str:
        """``label``, or the first ``label (n)`` not yet handed out."""
        if label not in self._used:
            self._used.add(label)
            return label
        n = self._next.get(label, 2)
        while f"{label} ({n})" in self._used:
            n += 1
        self._next[label] = n + 1
        name = f"{label} ({n})"
        self._used.add(name)
        return name


def _close(anchors: list[Anchor], size: int) -> list[Anchor]:
    """Each anchor ends where the next begins; the last at ``size``."""
    out: list[Anchor] = []
    for i, a in enumerate(anchors):
        end = anchors[i + 1].start if i + 1 < len(anchors) else size
        out.append(Anchor(a.label, a.start, max(end, a.start)))
    return out
