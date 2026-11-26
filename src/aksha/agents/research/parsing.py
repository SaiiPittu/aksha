"""Pure readers of the research corpus: a page, a review file or a note, into passages.

Every passage is a ``(start, end)`` range into the record's raw text, so the fetch tool
can ask the runtime to mint one citation span per passage, and each passage says what it
is (a price row, a specification, a marketing claim, hidden text). Nothing here reads a
file, a clock or a network: each function takes the text a tool's reader returned.

The page reader knows HTML only as far as product pages need: the ``published`` meta tag,
the title, the JSON-LD product and brand names, headings, paragraphs (``class="hero"`` is
marketing copy), table rows under each heading, and blocks hidden with ``display:none``.
Hidden text and text addressed to an automated reader is flagged ``instruction_like``: it
stays data, shown with its flag, and no rule ever acts on it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
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

_TAG = re.compile(r"<[^>]+>")
_PUBLISHED = re.compile(r'<meta name="published" content="([^"]+)">')
_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_NAME = re.compile(r'"name":\s*"([^"]+)"')
_BLOCK = re.compile(
    r"<(h1|h2|p|div)(\s[^>]*)?>(.*?)</\1>|<tr>(.*?)</tr>|<footer>.*?</footer>", re.S
)
_CELL = re.compile(r"<t([dh])>(.*?)</t\1>", re.S)
_RELATION = re.compile(
    r"\b(is the new name for|joined|acquired|was acquired by|merged with|now part of|renamed)\b",
    re.I,
)
_CLAIM = re.compile(r"\b(up to|claimed|claims|guaranteed|best[- ]in[- ]class)\b", re.I)
_INSTRUCTION = re.compile(
    r"\b(AI assistants?|language models?|ignore (all |your |previous )?instructions|"
    r"skip (the )?approval|send (the )?(full )?(packet|matrix|report))\b",
    re.I,
)
_MONEY = re.compile(r"^(?P<currency>[A-Z]{3}) (?P<amount>\d+(?:\.\d+)?) per (?P<per>.+)$")


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
    """A product page read into passages, with its identity fields."""

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


def _price_fields(text: str) -> dict[str, Any] | None:
    m = _MONEY.match(text.strip())
    if m is None:
        return None
    return {"currency": m["currency"], "amount": m["amount"], "per": m["per"]}


def _ld_passages(raw: str) -> Iterator[Passage]:
    for m in _LD.finditer(raw):
        body = m.group(1)
        base = m.start(1)
        try:
            data = json.loads(body)
        except ValueError:
            continue
        names = list(_NAME.finditer(body))
        brand = data.get("brand", {}) if isinstance(data, dict) else {}
        for n in names:
            kind = "brand" if isinstance(brand, dict) and n.group(1) == brand.get("name") else None
            if kind is None and isinstance(data, dict) and n.group(1) == data.get("name"):
                kind = "product"
            if kind is None:
                continue
            start, end = base + n.start(), base + n.end()
            yield Passage(kind, start, end, raw[start:end], fields={"name": n.group(1)})


def read_page(raw: str) -> Page:
    """``raw`` HTML as passages, in document order (module doc)."""
    passages: list[Passage] = []
    pub = _PUBLISHED.search(raw)
    published = pub.group(1) if pub else None
    if pub:
        passages.append(Passage("published", pub.start(1), pub.end(1), pub.group(1)))
    title_m = _TITLE.search(raw)
    title = visible(title_m.group(1)) if title_m else ""
    if title_m:
        s, e = _inner(raw, title_m, 1)
        passages.append(Passage("title", s, e, raw[s:e]))
    ld = list(_ld_passages(raw))
    passages.extend(ld)
    product = next((p.fields["name"] for p in ld if p.kind == "product"), None)
    brand = next((p.fields["name"] for p in ld if p.kind == "brand"), None)
    url_m = re.search(r'"url":\s*"([^"]+)"', raw)
    body_at = raw.find("<body")
    section = ""
    header: list[str] = []
    for m in _BLOCK.finditer(raw, max(body_at, 0)):
        if m.group(0).startswith("<footer"):
            continue
        if m.group(4) is not None:  # a table row
            cells = list(_CELL.finditer(m.group(4)))
            if not cells:
                continue
            if all(c.group(1) == "h" for c in cells):
                header = [visible(c.group(2)) for c in cells]
                continue
            values = [visible(c.group(2)) for c in cells]
            row = dict(zip(header, values, strict=False))
            second = cells[1] if len(cells) > 1 else cells[0]
            base = m.start(4)
            s, e = base + second.start(2), base + second.end(2)
            price = _price_fields(values[1]) if len(values) > 1 else None
            kind = "price" if price is not None else "spec"
            fields = {"row": row, **(price or {})}
            passages.append(Passage(kind, s, e, raw[s:e], section, fields))
            continue
        tag, attrs = m.group(1), m.group(2) or ""
        s, e = _inner(raw, m, 3)
        text = raw[s:e]
        if tag == "h1":
            passages.append(Passage("heading", s, e, text, fields={"level": 1}))
            continue
        if tag == "h2":
            section = visible(text)
            header = []
            passages.append(Passage("heading", s, e, text, fields={"level": 2}))
            continue
        hidden = "display:none" in attrs.replace(" ", "")
        flagged = bool(_INSTRUCTION.search(text))
        if hidden:
            kind = "hidden"
        elif _RELATION.search(text):
            kind = "relationship"
        elif 'class="hero"' in attrs or _CLAIM.search(text):
            kind = "claim"
        else:
            kind = "text"
        passages.append(Passage(kind, s, e, text, section, instruction_like=flagged or hidden))
    return Page(
        published=published,
        title=title,
        product=product,
        brand=brand,
        url=url_m.group(1) if url_m else None,
        passages=tuple(passages),
    )


def _text_ranges(raw: str) -> Iterator[tuple[str, int, int]]:
    """Each review row's ID with the range of its ``text`` value, from the raw JSON."""
    for m in re.finditer(r'"review_id":\s*"([^"]+)"', raw):
        t = re.compile(r'"text":\s*"((?:[^"\\]|\\.)*)"').search(raw, m.end())
        if t is not None:
            yield m.group(1), t.start(1), t.end(1)


def read_reviews(raw: str) -> tuple[dict[str, Any], tuple[Passage, ...]]:
    """The review file's header fields and one ``review`` passage per row, in file order."""
    data = json.loads(raw)
    rows = {str(r["review_id"]): r for r in data.get("reviews", ())}
    passages: list[Passage] = []
    for rid, s, e in _text_ranges(raw):
        row = rows.get(rid)
        if row is None:
            continue
        fields = {k: row[k] for k in sorted(row) if k not in ("text", "synthetic")}
        passages.append(Passage("review", s, e, raw[s:e], fields=fields))
    header = {k: data[k] for k in ("collected", "note") if k in data}
    return header, tuple(passages)


def read_lines(raw: str) -> tuple[Passage, ...]:
    """A text record's non-empty lines after any front matter, each a ``line`` passage."""
    start = 0
    if raw.startswith("---\n"):
        end = raw.find("\n---\n", 4)
        start = end + 5 if end >= 0 else 0
    out: list[Passage] = []
    pos = start
    for line in raw[start:].splitlines(keepends=True):
        body = line.rstrip("\n")
        s = pos + (len(body) - len(body.lstrip()))
        e = pos + len(body.rstrip())
        if e > s:
            out.append(Passage("line", s, e, raw[s:e]))
        pos += len(line)
    return tuple(out)
