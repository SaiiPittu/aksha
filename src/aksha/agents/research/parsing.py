"""Pure readers of any input record: a web page, a table, or a text, into citable passages.

Every passage is a ``(start, end)`` range into the record's raw text, so the read tools can
ask the runtime to mint one citation span per passage, and each passage says what it is
(a price row, a specification, a marketing claim, hidden text). Nothing here reads a file,
a clock or a network: each function takes the text a tool's reader returned.

The readers know formats, never a venture or a market:

1. :func:`input_format` names a record's format from its name: ``html``, ``markdown``,
   ``csv``, ``json``, ``email`` (``.eml``, ``.eml.txt``) or ``text`` (``.txt``, text
   extracted from a PDF as ``.pdf.txt``).
2. :func:`read_page` reads an HTML page: the publication date (a ``published`` or ``date``
   meta tag, or ``article:published_time``), the title, the product and its maker (JSON-LD
   ``Product`` and ``brand``, else ``og:title`` and ``og:site_name``), headings, paragraphs
   (``class="hero"`` is marketing copy), list items, table rows under each heading, and
   blocks hidden with ``display:none``. :func:`read_markdown_page` reads a Markdown page
   the same way (front matter ``published``/``date``, ``product``, ``company``/``brand``).
   Hidden text and text addressed to an automated reader is flagged ``instruction_like``:
   it stays data, shown with its flag, and no rule ever acts on it.
3. :func:`read_table` reads rows: a CSV file, or the list of objects in a JSON file (the
   file itself, or the first list of objects under a top-level key). Each row's text is
   its one citable passage; its other columns are named by what they hold (``id``,
   ``date``, ``product``, ``site``, ``geography``, ``rating``), whatever the export
   called them, so a review export from any site reads the same.
4. :func:`read_lines` reads any text as its non-empty lines after front matter.

A price is a money amount with its basis, as a page states it: ``EUR 249 per unit``,
``€249 / unit``, ``INR 1,499 per probe per season``, ``12% of order value``. The amount is
kept as written (``amount``, which the page's text holds) and as a plain number
(``number``, commas dropped), never converted.
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

PASSAGE_KINDS = (
    "published",
    "title",
    "product",
    "brand",
    "heading",
    "text",
    "relationship",
    "claim",
    "price",
    "spec",
    "hidden",
    "review",
    "line",
)

FORMATS = ("html", "markdown", "csv", "json", "email", "text")

_TAG = re.compile(r"<[^>]+>")
_PUBLISHED = re.compile(
    r'<meta (?:name|property)="(?:published|date|article:published_time)" content="([^"]+)">'
)
_OG = re.compile(r'<meta property="og:(title|site_name)" content="([^"]+)">')
_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_NAME = re.compile(r'"name":\s*"([^"]+)"')
_BLOCK = re.compile(
    r"<(h1|h2|h3|p|div|li)(\s[^>]*)?>(.*?)</\1>|<tr>(.*?)</tr>|<footer>.*?</footer>", re.S
)
_CELL = re.compile(r"<t([dh])>(.*?)</t\1>", re.S)
RELATION = re.compile(
    r"\b(is the new name for|joined|acquired|was acquired by|merged with|now part of|renamed|"
    r"is now|rebranded as|formerly)\b",
    re.I,
)
"""A sentence that says two names are one company: the only ground for a merge."""
_CLAIM = re.compile(r"\b(up to|claimed|claims|guaranteed|best[- ]in[- ]class|as fast as)\b", re.I)
_INSTRUCTION = re.compile(
    r"\b(AI assistants?|language models?|automated (?:readers?|agents?)|"
    r"ignore (all |your |previous )?instructions|skip (the )?approval|"
    r"send (the )?(full )?(packet|matrix|report|data))\b",
    re.I,
)
_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "₹": "INR"}
_MONEY = re.compile(
    r"^(?P<currency>[A-Z]{3}|[€$£₹])\s?(?P<amount>\d[\d,]*(?:\.\d+)?)\s*(?:per|/)\s*(?P<per>.+)$"
)
_PERCENT = re.compile(r"^(?P<amount>\d+(?:\.\d+)?)\s?% (?:of|per) (?P<per>.+)$")


def input_format(record_id: str) -> str:
    """The record's format from its name (module doc, item 1)."""
    name = record_id.rsplit("/", 1)[-1].lower()
    if name.endswith((".html", ".htm")):
        return "html"
    if name.endswith((".md", ".markdown")):
        return "markdown"
    if name.endswith(".csv"):
        return "csv"
    if name.endswith(".json"):
        return "json"
    if name.endswith((".eml", ".eml.txt")):
        return "email"
    return "text"


@dataclass(frozen=True)
class Passage:
    """One citable range of a record: ``text`` is ``raw[start:end]`` exactly."""

    kind: str
    start: int
    end: int
    text: str
    section: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    instruction_like: bool = False

    def as_json(self, n: int) -> dict[str, Any]:
        """The passage as a tool result shows it, numbered ``n`` in draft order."""
        out: dict[str, Any] = {"n": n, "kind": self.kind, "text": self.text}
        if self.section:
            out["section"] = self.section
        if self.fields:
            out["fields"] = dict(self.fields)
        if self.instruction_like:
            out["instruction_like"] = True
        return out


@dataclass(frozen=True)
class Page:
    """A page read into passages, with its identity fields."""

    published: str | None
    title: str
    product: str | None
    brand: str | None
    url: str | None
    passages: tuple[Passage, ...]


def visible(fragment: str) -> str:
    """``fragment`` with tags removed and whitespace runs collapsed."""
    return " ".join(_TAG.sub(" ", fragment).split())


def _inner(raw: str, m: re.Match[str], group: int) -> tuple[int, int]:
    """The range of group ``group`` of ``m``, trimmed of surrounding whitespace."""
    start, end = m.start(group), m.end(group)
    while start < end and raw[start].isspace():
        start += 1
    while end > start and raw[end - 1].isspace():
        end -= 1
    return start, end


def price_fields(text: str) -> dict[str, Any] | None:
    """A stated price's currency, amount (as written, commas dropped) and basis; else None."""
    stripped = " ".join(text.split())
    m = _MONEY.match(stripped)
    if m is not None:
        cur = _SYMBOLS.get(m["currency"], m["currency"])
        number = m["amount"].replace(",", "")
        return {"currency": cur, "amount": m["amount"], "number": number, "per": m["per"]}
    p = _PERCENT.match(stripped)
    if p is not None:
        return {"currency": "%", "amount": p["amount"], "number": p["amount"], "per": p["per"]}
    return None


def _ld_passages(raw: str) -> Iterator[Passage]:
    for m in _LD.finditer(raw):
        body = m.group(1)
        base = m.start(1)
        try:
            data = json.loads(body)
        except ValueError:
            continue
        brand = data.get("brand", {}) if isinstance(data, dict) else {}
        for n in _NAME.finditer(body):
            kind = "brand" if isinstance(brand, dict) and n.group(1) == brand.get("name") else None
            if kind is None and isinstance(data, dict) and n.group(1) == data.get("name"):
                kind = "product"
            if kind is None:
                continue
            start, end = base + n.start(1), base + n.end(1)
            yield Passage(kind, start, end, raw[start:end], fields={"name": n.group(1)})


def _og_passages(raw: str) -> Iterator[Passage]:
    for m in _OG.finditer(raw):
        kind = "product" if m.group(1) == "title" else "brand"
        yield Passage(kind, m.start(2), m.end(2), m.group(2), fields={"name": m.group(2)})


def _block_kind(text: str, attrs: str) -> tuple[str, bool]:
    hidden = "display:none" in attrs.replace(" ", "")
    flagged = bool(_INSTRUCTION.search(text)) or hidden
    if hidden:
        return "hidden", flagged
    if RELATION.search(text):
        return "relationship", flagged
    if 'class="hero"' in attrs or _CLAIM.search(text):
        return "claim", flagged
    return "text", flagged


def _row_passage(
    cells: list[tuple[str, int, int]], header: list[str], section: str
) -> Passage | None:
    """A table row: its second cell (or only cell) is the citable value."""
    if not cells:
        return None
    values = [c[0] for c in cells]
    row = dict(zip(header, values, strict=False)) if header else {}
    _, s, e = cells[1] if len(cells) > 1 else cells[0]
    price = None
    for text, cs, ce in cells[1:] or cells:
        price = price_fields(text)
        if price is not None:
            s, e = cs, ce
            break
    kind = "price" if price is not None else "spec"
    label = values[0] if len(values) > 1 else ""
    fields: dict[str, Any] = {"row": row, "label": label, **(price or {})}
    return Passage(kind, s, e, "", section, fields)


def read_page(raw: str) -> Page:
    """``raw`` HTML as passages, in document order (module doc, item 2)."""
    passages: list[Passage] = []
    pub = _PUBLISHED.search(raw)
    published = pub.group(1)[:10] if pub else None
    if pub:
        passages.append(Passage("published", pub.start(1), pub.end(1), pub.group(1)))
    title_m = _TITLE.search(raw)
    title = visible(title_m.group(1)) if title_m else ""
    if title_m:
        s, e = _inner(raw, title_m, 1)
        passages.append(Passage("title", s, e, raw[s:e]))
    names = list(_ld_passages(raw)) or list(_og_passages(raw))
    passages.extend(names)
    product = next((p.fields["name"] for p in names if p.kind == "product"), None)
    brand = next((p.fields["name"] for p in names if p.kind == "brand"), None)
    url_m = re.search(r'"url":\s*"([^"]+)"', raw) or re.search(
        r'<link rel="canonical" href="([^"]+)">', raw
    )
    body_at = raw.find("<body")
    section = ""
    header: list[str] = []
    for m in _BLOCK.finditer(raw, max(body_at, 0)):
        if m.group(0).startswith("<footer"):
            continue
        if m.group(4) is not None:  # a table row
            found = list(_CELL.finditer(m.group(4)))
            if found and all(c.group(1) == "h" for c in found):
                header = [visible(c.group(2)) for c in found]
                continue
            base = m.start(4)
            cells = [(visible(c.group(2)), base + c.start(2), base + c.end(2)) for c in found]
            row = _row_passage(cells, header, section)
            if row is not None:
                passages.append(_with_text(raw, row))
            continue
        tag, attrs = m.group(1), m.group(2) or ""
        s, e = _inner(raw, m, 3)
        text = raw[s:e]
        if tag in ("h1", "h2", "h3"):
            level = int(tag[1])
            if level >= 2:
                section, header = visible(text), []
            passages.append(Passage("heading", s, e, text, fields={"level": level}))
            continue
        if "<p" in text or "<li" in text or "<table" in text:
            continue  # a container: its own blocks are read one by one
        kind, flagged = _block_kind(text, attrs)
        passages.append(Passage(kind, s, e, text, section, instruction_like=flagged))
    return Page(
        published=published,
        title=title,
        product=product,
        brand=brand,
        url=url_m.group(1) if url_m else None,
        passages=tuple(passages),
    )


def _with_text(raw: str, p: Passage) -> Passage:
    return Passage(p.kind, p.start, p.end, raw[p.start : p.end], p.section, p.fields,
                   p.instruction_like)  # fmt: skip


def front_matter(raw: str) -> tuple[dict[str, str], int]:
    """A Markdown record's ``---`` front matter as strings, and where its body starts."""
    if not raw.startswith("---\n"):
        return {}, 0
    end = raw.find("\n---\n", 4)
    if end < 0:
        return {}, 0
    meta: dict[str, str] = {}
    for line in raw[4:end].splitlines():
        k, sep, v = line.partition(":")
        if sep:
            meta[k.strip().lower()] = v.strip().strip("'\"")
    return meta, end + 5


def _line_spans(raw: str, start: int) -> Iterator[tuple[int, int]]:
    """Each line's ``(start, end)`` in ``raw`` from ``start``, without its newline."""
    pos = start
    for line in raw[start:].splitlines(keepends=True):
        yield pos, pos + len(line.rstrip("\r\n"))
        pos += len(line)


def _table_cells(raw: str, s: int, e: int) -> list[tuple[str, int, int]]:
    out = []
    line = raw[s:e]
    bars = [i for i, ch in enumerate(line) if ch == "|"]
    for a, b in itertools.pairwise(bars):
        cs, ce = s + a + 1, s + b
        while cs < ce and raw[cs].isspace():
            cs += 1
        while ce > cs and raw[ce - 1].isspace():
            ce -= 1
        out.append((raw[cs:ce], cs, ce))
    return out


_META_KINDS = {"published": "published", "date": "published", "product": "product",
               "company": "brand", "brand": "brand"}  # fmt: skip


def _meta_passages(raw: str, body: int) -> Iterator[Passage]:
    """Front matter values that say who a page is and when, each a citable passage."""
    seen: set[str] = set()
    for m in re.finditer(r"^([A-Za-z_]+):[ \t]*['\"]?(.*?)['\"]?[ \t]*$", raw[:body], re.M):
        kind = _META_KINDS.get(m.group(1).lower())
        if kind is None or kind in seen or not m.group(2):
            continue
        seen.add(kind)
        fields = {"name": m.group(2)} if kind in ("product", "brand") else {}
        yield Passage(kind, m.start(2), m.end(2), m.group(2), fields=fields)


def read_markdown_page(raw: str) -> Page:
    """A Markdown page as passages: headings, paragraphs, list items and table rows."""
    meta, body = front_matter(raw)
    passages: list[Passage] = list(_meta_passages(raw, body))
    section = ""
    header: list[str] = []
    title = ""
    for s, e in _line_spans(raw, body):
        line = raw[s:e]
        text = line.strip()
        if not text or (text.startswith(">") and "SYNTHETIC" in text.upper()):
            continue
        lead = len(line) - len(line.lstrip())
        s += lead
        if text.startswith("#"):
            level = len(text) - len(text.lstrip("#"))
            hs = s + level + (1 if raw[s + level : s + level + 1] == " " else 0)
            heading = raw[hs:e]
            if level == 1 and not title:
                title = heading
            if level >= 2:
                section, header = heading, []
            passages.append(Passage("heading", hs, e, heading, fields={"level": level}))
            continue
        if text.startswith("|"):
            cells = _table_cells(raw, s, e)
            if all(re.fullmatch(r":?-{3,}:?", c[0]) for c in cells):
                continue
            if not header:
                header = [c[0] for c in cells]
                continue
            row = _row_passage(cells, header, section)
            if row is not None:
                passages.append(_with_text(raw, row))
            continue
        if re.match(r"^([-*]|\d+\.) ", text):
            s += len(re.match(r"^([-*]|\d+\.) ", text).group(0))  # type: ignore[union-attr]
        kind, flagged = _block_kind(raw[s:e], "")
        passages.append(Passage(kind, s, e, raw[s:e], section, instruction_like=flagged))
    published = meta.get("published") or meta.get("date")
    return Page(
        published=published[:10] if published else None,
        title=meta.get("title", title),
        product=meta.get("product") or None,
        brand=meta.get("company") or meta.get("brand") or None,
        url=meta.get("url") or None,
        passages=tuple(passages),
    )


# --- tables -------------------------------------------------------------------------------

COLUMN_ROLES: Mapping[str, tuple[str, ...]] = {
    "id": ("review_id", "id", "review", "ref", "reference", "row_id"),
    "text": ("text", "review_text", "body", "comment", "comments", "content", "feedback",
             "message", "review"),
    "date": ("date", "posted", "posted_on", "created", "submitted", "published"),
    "product": ("product", "item", "service", "app", "brand", "company", "vendor"),
    "site": ("site", "source", "platform", "channel", "store"),
    "geography": ("geography", "country", "region", "market", "location", "city"),
    "rating": ("rating", "stars", "score"),
    "rating_scale_max": ("rating_scale_max", "scale", "out_of", "max_rating"),
    "rating_unit": ("rating_unit", "unit"),
}  # fmt: skip
"""The column names each role goes by, most specific first: what a column holds, not where."""


def column_roles(columns: Sequence[str]) -> dict[str, str]:
    """Each role's column among ``columns`` (case and spaces folded), first match wins."""
    folded = {re.sub(r"[^a-z0-9]+", "_", c.lower()).strip("_"): c for c in columns}
    out: dict[str, str] = {}
    taken: set[str] = set()
    for role, names in COLUMN_ROLES.items():
        for n in names:
            col = folded.get(n)
            if col is not None and col not in taken:
                out[role] = col
                taken.add(col)
                break
    return out


@dataclass(frozen=True)
class Table:
    """Rows read from a table record: the column roles and one passage per row's text."""

    columns: tuple[str, ...]
    roles: Mapping[str, str]
    passages: tuple[Passage, ...]
    header: Mapping[str, Any] = field(default_factory=dict)


def _csv_cells(raw: str) -> Iterator[list[tuple[str, int, int]]]:
    """RFC 4180 rows of ``raw``, each cell with its value and its raw range (quotes out)."""
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
        else:
            j = i
            while j < n and raw[j] not in ",\r\n":
                j += 1
            row.append((raw[i:j], i, j))
            i = j
        if i >= n or raw[i] in "\r\n":
            if any(v for v, _, _ in row):
                yield row
            row = []
            if i < n and raw[i] == "\r":
                i += 1
            i += 1
        else:
            i += 1  # the comma


def _text_role(roles: Mapping[str, str], rows: Sequence[Mapping[str, Any]]) -> str | None:
    if "text" in roles:
        return roles["text"]
    lengths: dict[str, int] = {}
    for r in rows:
        for k, v in r.items():
            if isinstance(v, str):
                lengths[k] = lengths.get(k, 0) + len(v)
    return max(lengths, key=lambda k: lengths[k]) if lengths else None


def _row_fields(
    row: Mapping[str, Any], roles: Mapping[str, str], text: str, n: int
) -> dict[str, Any]:
    out: dict[str, Any] = {r: row[c] for r, c in roles.items() if r != "text" and c in row}
    out.setdefault("id", f"row-{n + 1}")
    out["columns"] = {k: v for k, v in row.items() if k != text and k.lower() != "synthetic"}
    return out


def read_table(raw: str, fmt: str) -> Table:
    """``raw`` (a ``csv`` or ``json`` record) as rows (module doc, item 3)."""
    if fmt == "csv":
        rows_cells = list(_csv_cells(raw))
        if not rows_cells:
            return Table((), {}, ())
        columns = tuple(v for v, _, _ in rows_cells[0])
        dict_rows = [dict(zip(columns, (v for v, _, _ in r), strict=False)) for r in rows_cells[1:]]
        roles = column_roles(columns)
        text = _text_role(roles, dict_rows)
        passages = []
        if text is not None:
            at = columns.index(text)
            for n, (cells, row) in enumerate(zip(rows_cells[1:], dict_rows, strict=True)):
                if at < len(cells):
                    _, s, e = cells[at]
                    fields = _row_fields(row, roles, text, n)
                    passages.append(Passage("review", s, e, raw[s:e], fields=fields))
        return Table(columns, {**roles, "text": text or ""}, tuple(passages))
    data = json.loads(raw)
    header: dict[str, Any] = {}
    rows: list[Mapping[str, Any]] = []
    if isinstance(data, list):
        rows = [r for r in data if isinstance(r, Mapping)]
    elif isinstance(data, Mapping):
        for _k, v in data.items():
            if isinstance(v, list) and v and all(isinstance(r, Mapping) for r in v):
                rows = list(v)
                break
        header = {k: v for k, v in data.items() if isinstance(v, str | int | float)}
    columns = tuple(dict.fromkeys(k for r in rows for k in r))
    jroles = column_roles(columns)
    jtext = _text_role(jroles, rows)
    passages = []
    if jtext is not None:
        key = re.compile(rf'"{re.escape(jtext)}"\s*:\s*"((?:[^"\\]|\\.)*)"')
        pos = 0
        for n, jrow in enumerate(rows):
            jvalue = jrow.get(jtext)
            if not isinstance(jvalue, str):
                continue
            m = key.search(raw, pos)
            while m is not None and json.loads(f'"{m.group(1)}"') != jvalue:
                m = key.search(raw, m.end())
            if m is None:
                continue
            pos = m.end()
            fields = _row_fields(jrow, jroles, jtext, n)
            passages.append(Passage("review", m.start(1), m.end(1), m.group(1), fields=fields))
    header.pop("synthetic", None)
    return Table(columns, {**jroles, "text": jtext or ""}, tuple(passages), header)


def read_lines(raw: str) -> tuple[Passage, ...]:
    """A text record's non-empty lines after any front matter, each a ``line`` passage."""
    _, start = front_matter(raw)
    out: list[Passage] = []
    for s, e in _line_spans(raw, start):
        body = raw[s:e]
        ls = s + (len(body) - len(body.lstrip()))
        le = s + len(body.rstrip())
        if le > ls:
            out.append(Passage("line", ls, le, raw[ls:le]))
    return tuple(out)


SHAPES = ("seller_page", "review_table", "table", "note")
"""What an input looks like it holds, from its content (:func:`shape_of`)."""


def shape_of(record_id: str, raw: str) -> str:
    """What ``raw`` holds, by structure alone, never by a name it carries.

    A page (HTML or Markdown) that names a product or its maker, or states a price, is a
    ``seller_page``. A table with a text column and a rating column is a ``review_table``
    (reviews rate; other tables with text do not); any other table is a ``table``.
    Everything else is a ``note``.
    """
    fmt = input_format(record_id)
    try:
        if fmt in ("html", "markdown"):
            page = read_page(raw) if fmt == "html" else read_markdown_page(raw)
            priced = any(p.kind == "price" for p in page.passages)
            return "seller_page" if page.product or page.brand or priced else "note"
        if fmt in ("csv", "json"):
            table = read_table(raw, fmt)
            explicit = column_roles(table.columns)
            rows = len(table.passages)
            return (
                "review_table" if "text" in explicit and "rating" in explicit and rows else "table"
            )
    except ValueError:
        return "note"
    return "note"
