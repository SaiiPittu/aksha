"""The research agent's offline fast-class models: rule-based readers, test doubles only.

Each implements one alias's model-class contract (``reading.py``) from what the request
carries, the passages or the state code rendered, and nothing else, so ``fake`` mode is
offline and writes the same bytes twice. They are general rules a careful reader applies
to any document; none names a fixture. A live model replaces every one of them, and the
agent code never sees which answered.

* :class:`PageFacts` (``page_facts``): identity from structured data in any form
  (JSON-LD as an object, a list or a ``@graph``, ``brand`` as a string or an object;
  ``og:`` tags in any attribute order; ``key: value`` front matter, headers and lines),
  else "<product> by <company>" in the title; the publication date from a meta tag, a
  header or a "published"/"updated" phrase, in any common date form; prices wherever a
  money amount sits in a table row, a pricing card or a sentence with its basis;
  relationship sentences; marketing claims; text addressed to an automated reader.
* :class:`TableRoles` (``table_roles``): a column's role from the words of its name and,
  when those say nothing, from its values (dates, small numbers, country codes, long text).
* :class:`FeedbackItems` (``feedback_items``): each quoted or listed customer voice of a text.
* :class:`ThemeMatch` (``theme_match``): a theme by its terms, else by its name's words.
* :class:`DocKind` (``doc_kind``): the decide step, a record's kind from the structural
  preview code rendered (:func:`decide_record`).
* :class:`PriceTable` (``price_table``) and :class:`ReportFacts` (``report_facts``): the
  schemas of a price list and of a third party's market report.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from decimal import Decimal
from typing import Any

from aksha.runtime.api import (
    ClassAnswer,
    ClassificationRequest,
    ClassificationResult,
    ExtractionRequest,
    ExtractionResult,
    FieldAnswer,
    FieldSpec,
    QuestionSpec,
    RuleClassificationModel,
    RuleExtractionModel,
    SpanRef,
    distribution_ppm,
    find_dates,
    parse_date,
    ppm,
)

from . import analysis, parsing

SURE = Decimal("0.95")
ABSENT = ppm(Decimal("0.9"))


_CELL_TAG = re.compile(r"</?(td|th|tr|table|p|div|li|br|h[1-6]|ul|ol|section|article)\b", re.I)


def clean_map(raw: str) -> tuple[str, list[int]]:
    """``raw`` without tags (entities kept), and each kept character's index in ``raw``.

    A block or cell tag becomes ``|`` (a boundary no value crosses), an inline tag a space.
    """
    out: list[str] = []
    where: list[int] = []
    i = 0
    while i < len(raw):
        if raw[i] == "<":
            j = raw.find(">", i)
            if j > 0:
                sep = "|" if _CELL_TAG.match(raw, i) else " "
                if not out or out[-1] not in (" ", "|") or sep == "|":
                    out.append(sep)
                    where.append(i)
                i = j + 1
                continue
        out.append(raw[i])
        where.append(i)
        i += 1
    return "".join(out), where


def _raw_range(where: list[int], s: int, e: int, clean: str = "") -> tuple[int, int]:
    """The raw range of ``clean[s:e]``, trimmed of the spaces and boundaries at its ends."""
    if clean:
        while s < e and clean[s] in " |":
            s += 1
        while e > s and clean[e - 1] in " |":
            e -= 1
    return where[s], where[e - 1] + 1


class _Found:
    """Answers being collected: field name to values and spans."""

    def __init__(self) -> None:
        self.items: dict[str, list[tuple[str, SpanRef]]] = {}

    def add(self, name: str, value: str, span: str, s: int, e: int) -> None:
        if e <= s:
            return
        got = self.items.setdefault(name, [])
        if any(r.span_id == span and r.start == s and r.end == e for _, r in got):
            return
        got.append((value, SpanRef(span, s, e)))

    def answer(self, spec: FieldSpec) -> FieldAnswer:
        got = self.items.get(spec.name, [])
        if not got:
            return FieldAnswer(None, (), p_null_ppm=ABSENT)
        if spec.type == "list":
            return FieldAnswer(tuple(v for v, _ in got), tuple(r for _, r in got),
                               p_null_ppm=ppm(1 - SURE), confidence_ppm=ppm(SURE))  # fmt: skip
        value, ref = got[0]
        return FieldAnswer(value, (ref,), p_null_ppm=ppm(1 - SURE), confidence_ppm=ppm(SURE))


# --- page facts ----------------------------------------------------------------------------

_ATTR = re.compile(r'([\w:.-]+)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))')
DATE_NAMES = frozenset({"published", "date", "article:published_time", "datepublished",
                        "pubdate", "publish-date", "publish_date", "dc.date", "created",
                        "issued", "release_date", "og:published_time"})  # fmt: skip
PRODUCT_KEYS = ("product", "product name", "item", "offering", "model", "solution")
COMPANY_KEYS = ("company", "brand", "vendor", "maker", "manufacturer", "seller", "supplier",
                "provider", "from", "by", "organisation", "organization")  # fmt: skip
_KV = re.compile(
    r"^\s*(?:[-*]\s*)?\**([A-Za-z][A-Za-z _]{1,24}?)\**\s*[:\uff1a\u2013-]\s*\**(.+?)\**\s*$"
)
DATE_WORDS = frozenset({"published", "updated", "issued", "dated", "saved", "captured",
                        "retrieved", "modified", "revised"})  # fmt: skip
"""Words of a key that names when the page was written or captured."""
GENERIC_TITLE = re.compile(
    r"^(?:pricing|prices|plans?|price ?list|datasheet|data sheet|brochure|home)"
    r"\s*[:\u2013\u2014-]?\s+"
    r"|\s+(?:pricing|prices|plans?(?: and pricing)?|price ?list|datasheet|data sheet|brochure)$",
    re.I,
)
"""A page title's words about the page, not the offer: "Brackpond plans" names Brackpond."""
_TITLE_SPLIT = re.compile(r"\s+[-|\u2013\u2014:]\s+")
_BY = re.compile(
    r"^(?P<product>[^|:\u2014\u2013]{2,60}?)\s+(?:by|from)\s+(?P<company>[A-Z][\w&.' -]{1,50}?)\s*$"
)
_PUBLISHED_PHRASE = re.compile(
    r"\b(published|posted|updated|last updated|dated|issued|as of|effective|date(?=\s*:))\b[:\s]+",
    re.I,
)
RELATION = re.compile(
    r"\b(is the new name for|was renamed|renamed (?:to|as|itself)|rebranded as|formerly known as|"
    r"is (?:now )?called|is a trading name of|trades as|(?:is )?now trading as|"
    r"is now (?=[A-Z])|"
    r"formerly|was acquired by|acquired|merged with|(?:is )?now part of|is now a brand of|"
    r"is now owned by|joined (?=(?:the )?[A-Z][\w&-]*(?: [A-Z][\w&-]*)* "
    r"(?:Group|Holdings|Inc|Ltd|GmbH|BV|AG|SA|plc|Corp|Corporation|Company)\b))",
)
"""A sentence that says two names are one company. ``joined`` counts only when what is
joined is a company (a capitalised name ending in a company word), never a person."""
CLAIM = re.compile(
    r"\b(up to|claimed|claims|guaranteed|best[- ]in[- ]class|as fast as|industry[- ]leading|"
    r"world'?s first|market[- ]leading|unmatched|unbeatable|the only)\b",
    re.I,
)
ADDRESSED = re.compile(
    r"\b(AI assistants?|language models?|LLMs?|automated (?:readers?|agents?|tools?)|"
    r"ignore (?:all |your |any |previous |prior )*instructions|skip (?:the )?approvals?|"
    r"send (?:the |this )?(?:full )?(?:packet|matrix|report|data|file|results))\b",
    re.I,
)
NOT_A_PRICE = re.compile(
    r"\b(save[sd]?|saving|worth|revenue|raised|funding|loss|losses|wasted?|turnover|"
    r"budget|grant|fine|penalty)\b[^.]{0,30}$",
    re.I,
)
_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?](?=\s|$)|$)")


def _sentences(text: str) -> Iterator[tuple[int, int]]:
    for m in _SENTENCE.finditer(text):
        s, e = m.start(), m.end()
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e > s:
            yield s, e


def _attrs(tag: str) -> dict[str, str]:
    return {m.group(1).lower(): (m.group(3) or m.group(4) or m.group(5) or "")
            for m in _ATTR.finditer(tag)}  # fmt: skip


def _attr_value_range(tag: str, value: str) -> tuple[int, int] | None:
    for m in _ATTR.finditer(tag):
        for g in (3, 4, 5):
            if m.group(g) == value and value:
                return m.start(g), m.end(g)
    return None


def _ld_objects(data: Any) -> Iterator[dict[str, Any]]:
    if isinstance(data, list):
        for d in data:
            yield from _ld_objects(d)
    elif isinstance(data, dict):
        yield data
        for k in ("@graph", "itemListElement", "mainEntity", "offers"):
            if k in data:
                yield from _ld_objects(data[k])


def _types(obj: dict[str, Any]) -> set[str]:
    t = obj.get("@type", "")
    return {str(x).lower() for x in (t if isinstance(t, list) else [t])}


def _name_of(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict) and isinstance(value.get("name"), str):
        return str(value["name"]).strip()
    if isinstance(value, list) and value:
        return _name_of(value[0])
    return None


def _quoted(text: str, value: str) -> tuple[int, int] | None:
    """Where ``value`` sits in ``text`` as a JSON string, else anywhere."""
    m = re.search(r'"' + re.escape(json.dumps(value)[1:-1]) + r'"', text)
    if m:
        return m.start() + 1, m.end() - 1
    at = text.find(value)
    return (at, at + len(value)) if at >= 0 else None


def _is_row(text: str) -> bool:
    return (
        "</td>" in text or "</th>" in text or text.lstrip().startswith("|") or text.count("|") >= 2
    )


def _price_ok(clean: str, m: re.Match[str], card: bool) -> bool:
    if NOT_A_PRICE.search(clean[max(0, m.start() - 40) : m.start()]):
        return False
    stated = parsing.price_fields(m.group(0))
    if stated is None:
        return False
    return bool(stated["per"] or stated["condition"] or card)


def _lines_of(state: Sequence[Any]) -> Iterator[tuple[Any, int, str, int]]:
    """Each line of each passage: the span, the line's offset, its stripped text, its lead.

    A hard-wrapped paragraph (text extracted from a PDF) keeps a title line apart from the
    line after it, so "<product> by <company>" is read on its own line.
    """
    for span in state:
        at = 0
        for line in span.text.split("\n"):
            stripped = line.strip()
            yield span, at, stripped, len(line) - len(line.lstrip())
            at += len(line) + 1


def _legal_name_line(text: str) -> bool:
    """Whether a line is only a company's legal name (the maker's line, not the offer's)."""
    m = CORPORATE.match(text)
    return m is not None and m.end() >= len(text.rstrip(". ")) - 1


class PageFacts(RuleExtractionModel):
    """``page_facts`` by general reading rules (module doc)."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """Every field the request names, from its passages."""
        found = _Found()
        state = request.state
        self._structured(state, found)
        for span in state:
            text = span.text
            if text.startswith("<meta") or text.startswith("<link"):
                continue
            clean, where = clean_map(text)
            card = _is_row(text) or len(clean.split()) <= 8
            for m in parsing.MONEY.finditer(clean):
                if _price_ok(clean, m, card):
                    s, e = _raw_range(where, m.start(), m.end(), clean)
                    found.add("prices", text[s:e], span.span_id, s, e)
            if card:
                for m in parsing.PERCENT.finditer(clean):
                    s, e = _raw_range(where, m.start(), m.end(), clean)
                    found.add("prices", text[s:e], span.span_id, s, e)
            if span.text.lstrip().startswith(("{", "[")):
                continue
            for s0, e0 in _sentences(clean):
                sentence = clean[s0:e0]
                s, e = _raw_range(where, s0, e0, clean)
                if RELATION.search(sentence):
                    found.add("relationships", text[s:e], span.span_id, s, e)
                if CLAIM.search(sentence):
                    found.add("claims", text[s:e], span.span_id, s, e)
                if ADDRESSED.search(sentence):
                    found.add("addressed_to_agents", text[s:e], span.span_id, s, e)
        return ExtractionResult(tuple((f.name, found.answer(f)) for f in request.fields))

    def _structured(self, state: Sequence[Any], found: _Found) -> None:
        """Identity and date from structured data first, then lines, then the title."""
        for span in state:
            text = span.text
            if text.startswith("<meta"):
                a = _attrs(text)
                key = (a.get("name") or a.get("property") or a.get("itemprop") or "").lower()
                content = a.get("content", "")
                at = _attr_value_range(text, content)
                if at is None:
                    continue
                if key in DATE_NAMES and parse_date(content[:10]):
                    found.add("published", parse_date(content[:10]) or "", span.span_id, *at)
                elif key in ("og:title", "product:name", "twitter:title"):
                    found.add("product", content, span.span_id, *at)
                elif key in ("og:site_name", "author", "publisher", "application-name"):
                    found.add("company", content, span.span_id, *at)
            elif text.lstrip().startswith(("{", "[")):
                try:
                    data = json.loads(text)
                except ValueError:
                    continue
                for obj in _ld_objects(data):
                    types = _types(obj)
                    name = _name_of(obj.get("name"))
                    if "product" in types or "softwareapplication" in types or "service" in types:
                        if name and (at := _quoted(text, name)):
                            found.add("product", name, span.span_id, *at)
                        maker = _name_of(obj.get("brand")) or _name_of(obj.get("manufacturer"))
                        if maker and (at := _quoted(text, maker)):
                            found.add("company", maker, span.span_id, *at)
                    elif (
                        types & {"organization", "corporation", "localbusiness"}
                        and name
                        and (at := _quoted(text, name))
                    ):
                        found.add("company", name, span.span_id, *at)
                    date = obj.get("datePublished") or obj.get("dateCreated")
                    if (
                        isinstance(date, str)
                        and parse_date(date[:10])
                        and (at := _quoted(text, date))
                    ):
                        found.add("published", parse_date(date[:10]) or "", span.span_id, *at)
        for span in state:  # a JSON feed's own fields: "vendor": {"name": ...}, "product": ...
            m = re.match(
                r'^"([\w .-]+)"\s*:\s*(?:\{\s*"([\w .-]+)"\s*:\s*)?"(.*)"$', span.text, re.S
            )
            if m is None:
                continue
            words = set(name_words(m.group(1))) | set(name_words(m.group(2) or ""))
            m = re.match(r'^(?:.*?):\s*(?:\{\s*"[\w .-]+"\s*:\s*)?"(?P<v>.*)"$', span.text, re.S)
            if m is None:
                continue
            value, vs = m.group("v"), m.start("v")
            if words & {"product", "offering", "solution"}:
                found.add("product", value, span.span_id, vs, vs + len(value))
            elif words & {"vendor", "company", "brand", "maker", "manufacturer", "seller",
                          "provider", "supplier", "organisation", "organization"}:  # fmt: skip
                if "name" in words or len(words) == 1:
                    found.add("company", value, span.span_id, vs, vs + len(value))
            elif words & (DATE_WORDS | {"date"}) and parse_date(value[:10]):
                found.add("published", parse_date(value[:10]) or "", span.span_id, vs,
                          vs + len(value))  # fmt: skip
        for span in state:
            text = span.text
            if text.startswith("<") or len(text) > 200:
                continue
            m = _KV.match(text)
            if m is None:
                continue
            key = m.group(1).strip().lower()
            value = m.group(2).strip().strip("'\"")
            vs = text.find(value, m.start(2))
            if vs < 0 or not value:
                continue
            ve = vs + len(value)
            if key in PRODUCT_KEYS:
                found.add("product", value, span.span_id, vs, ve)
            elif key in COMPANY_KEYS:
                found.add("company", value, span.span_id, vs, ve)
            elif (
                key in ("published", "date", "updated", "issued", "sent", "dated")
                or set(name_words(key)) & DATE_WORDS
            ):
                dates = find_dates(value, "dmy")
                if dates:
                    iso, ds, de = dates[0]
                    found.add("published", iso, span.span_id, vs + ds, vs + de)
        for span in state:
            clean, where = clean_map(span.text)
            for m in _PUBLISHED_PHRASE.finditer(clean):
                dates = find_dates(clean[m.end() : m.end() + 40], "dmy")
                if dates and dates[0][1] <= 1:
                    iso, ds, de = dates[0]
                    s, e = _raw_range(where, m.end() + ds, m.end() + de, clean)
                    found.add("published", iso, span.span_id, s, e)
        if "company" not in found.items:
            legal: dict[str, tuple[Any, int, int]] = {}
            for span in state:
                if span.text.startswith("<meta"):
                    continue
                for m in CORPORATE.finditer(span.text):
                    legal.setdefault(m.group(0).lower(), (span, m.start(), m.end()))
            if len(legal) == 1:  # one company's legal name: the company whose page it is
                ((span, s, e),) = legal.values()
                found.add("company", span.text[s:e], span.span_id, s, e)
        if "product" not in found.items or "company" not in found.items:
            self._title(state, found)
        if "product" not in found.items or "company" not in found.items:
            for span, line_at, text, lead in _lines_of(state[:6]):
                if text.startswith("<") or len(text) > 90:
                    continue
                m = _BY.match(text)
                if m is None:
                    continue
                lead += line_at
                if "product" not in found.items:
                    p = m.group("product").strip()
                    found.add("product", p, span.span_id, lead + m.start("product"),
                              lead + m.start("product") + len(p))  # fmt: skip
                if "company" not in found.items:
                    c = m.group("company").strip()
                    found.add("company", c, span.span_id, lead + m.start("company"),
                              lead + m.start("company") + len(c))  # fmt: skip
                break

    def _title(self, state: Sequence[Any], found: _Found) -> None:
        """The offer's name from the page's title line, when nothing structured names it.

        The title is a ``title:`` line, else the first short line that is not a sentence.
        A title of parts ("Pricing - Siltgrove Logs") names the offer in the part that shares
        a word with the company's name, else in its first part; words about the page itself
        ("plans", "pricing", "datasheet") are not part of the name.
        """
        if "product" in found.items:
            return
        company = found.items.get("company", [("", None)])[0][0].lower().split()
        title: tuple[Any, int, int] | None = None
        for span, line_at, text, lead in _lines_of(state[:12]):
            m = _KV.match(text)
            if m and m.group(1).strip().lower() == "title":
                at = line_at + lead + text.find(m.group(2).strip())
                title = (span, at, at + len(m.group(2).strip()))
                break
            if (
                title is None
                and text
                and "<" not in text
                and "synthetic" not in text.lower()
                and len(text.split()) <= 8
                and not re.search(r"[.!?,;]$", text)
                and not re.match(r"^[\w ]{1,24}:\s", text)
                and re.search(r"[A-Za-z]{3}", text)
                and not _legal_name_line(text)
            ):
                title = (span, line_at + lead, line_at + lead + len(text))
        if title is None:
            return
        span, s, e = title
        text = span.text[s:e]
        parts: list[tuple[int, int]] = []
        cut = 0
        for m in _TITLE_SPLIT.finditer(text):
            parts.append((cut, m.start()))
            cut = m.end()
        parts.append((cut, len(text)))
        first = company[0] if company else ""
        pick = next((p for p in parts if first and first in text[p[0] : p[1]].lower()), parts[0])
        ps, pe = pick
        while True:
            m = GENERIC_TITLE.search(text[ps:pe])
            if m is None or m.end() - m.start() >= pe - ps:
                break
            if m.start() == 0:
                ps += m.end()
            else:
                pe = ps + m.start()
        if pe > ps:
            found.add("product", text[ps:pe].strip(), span.span_id, s + ps, s + pe)


# --- tables ---------------------------------------------------------------------------------

ROLE_WORDS: dict[str, frozenset[str]] = {
    "id": frozenset({"id", "ref", "reference", "uuid", "key", "number", "no"}),
    "text": frozenset(
        {
            "text",
            "body",
            "comment",
            "comments",
            "content",
            "feedback",
            "message",
            "review",
            "verbatim",
            "answer",
            "response",
            "quote",
            "remarks",
        }
    ),
    "date": frozenset(
        {
            "date",
            "posted",
            "created",
            "submitted",
            "published",
            "time",
            "timestamp",
            "when",
            "day",
            "on",
        }
    ),
    "product": frozenset(
        {
            "product",
            "item",
            "service",
            "app",
            "brand",
            "company",
            "vendor",
            "model",
            "sku",
            "offering",
            "tool",
        }
    ),
    "site": frozenset({"site", "source", "platform", "channel", "store", "marketplace", "via"}),
    "geography": frozenset(
        {
            "geography",
            "country",
            "region",
            "market",
            "location",
            "city",
            "geo",
            "nation",
            "territory",
        }
    ),
    "rating": frozenset({"rating", "stars", "star", "score", "rate", "nps", "grade", "overall"}),
    "rating_scale": frozenset({"scale", "max", "out", "best", "outof", "of"}),
    "rating_basis": frozenset({"unit"}),
}
_COUNTRY = re.compile(r"^[A-Z]{2}$")


def name_words(name: str) -> list[str]:
    """A column name's words: split on case changes, digits, spaces and punctuation."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return [w for w in re.split(r"[^a-z]+", spaced.lower()) if w]


def guess_role(name: str, values: Sequence[str]) -> str:
    """The role of a column named ``name`` holding ``values`` (module doc)."""
    words = name_words(name)
    last = words[-1] if words else ""
    vals = [v for v in values if v]
    avg = sum(len(v) for v in vals) / len(vals) if vals else 0
    numeric = vals and all(re.fullmatch(r"\d+(\.\d+)?(\s*/\s*\d+)?|★+☆*", v) for v in vals)
    if last == "id" or (words and words[0] == "id") or last in ("ref", "uuid"):
        return "id"
    varied = len(set(vals)) > 1
    if {"scale", "max"} & set(words) or ("out" in words and "of" in words):
        if numeric and varied and set(words) & ROLE_WORDS["rating"]:
            return "rating"  # "score out of 10" holding scores, not the scale's maximum
        return "rating_scale" if numeric else "other"
    if "unit" in words and "rating" in words:
        return "rating_basis"
    for role in ("rating", "date", "geography", "site", "product"):
        if set(words) & ROLE_WORDS[role]:
            if role == "rating" and vals and not numeric:
                continue
            if role == "date" and vals and not all(parse_date(v[:10]) for v in vals):
                continue
            return role
    if set(words) & ROLE_WORDS["text"]:
        return "text" if avg >= 25 or not vals else "id"
    if set(words) & ROLE_WORDS["id"] and avg < 25:
        return "id"
    if vals and all(parse_date(v[:10]) for v in vals):
        return "date"
    if vals and all(_COUNTRY.match(v) for v in vals):
        return "geography"
    return "other"


def _columns(state: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in state.splitlines():
        if not line.startswith("- "):
            continue
        name, _, values = line[2:].partition(": ")
        out[name] = [v.strip() for v in values.split(" | ") if v.strip()] if values else []
    return out


def _codes(values: Sequence[str]) -> bool:
    vals = [v for v in values if v]
    return bool(vals) and all(_COUNTRY.match(v) for v in vals)


def _choice(spec: QuestionSpec, chosen: str) -> ClassAnswer:
    rest = (1 - SURE) / max(1, len(spec.options) - 1)
    weights = {o: SURE if o == chosen else rest for o in spec.options}
    return ClassAnswer(chosen, distribution_ppm(weights))


def _boolean(value: bool) -> ClassAnswer:
    p = SURE if value else 1 - SURE
    return ClassAnswer(value, distribution_ppm({"true": p, "false": 1 - p}))


class TableRoles(RuleClassificationModel):
    """``table_roles``: each column's role from its name's words, else its values."""

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """One role per column question."""
        columns = _columns(request.state)
        named = []
        for q in request.questions:
            m = re.search(r"column '(.*)' hold|column \"(.*)\" hold", q.prompt)
            name = (m.group(1) or m.group(2)) if m else ""
            named.append((q, name, guess_role(name, columns.get(name, []))))
        coded = [n for _, n, r in named if r == "geography" and _codes(columns.get(n, []))]
        out = []
        for q, name, role in named:
            if role == "geography" and coded and name not in coded:
                role = "other"  # a region or city beside a country code: the code is the place
            out.append((q.name, _choice(q, role if role in q.options else "other")))
        return ClassificationResult(tuple(out))


# --- customer voices --------------------------------------------------------------------------

_QUOTE = re.compile(r"[“\"]([^”\"<>=]{12,600})[”\"]")
_SPEAKER = re.compile(r"^\s*(?:[A-Z][\w.-]{0,20}|P\d+|R\d+|Q\d*|A\d*)\s*[:\u2014\u2013-]\s+")


class FeedbackItems(RuleExtractionModel):
    """``feedback_items``: each quoted voice; else each listed or spoken line of a text."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """The ``items`` field, one span per voice, in document order."""
        found = _Found()
        quoted = False
        for span in request.state:
            for m in _QUOTE.finditer(span.text):
                if (m.start() and span.text[m.start() - 1] == "=") or " " not in m.group(1):
                    continue  # an attribute's value in markup, not someone's words
                quoted = True
                found.add("items", m.group(1), span.span_id, m.start(1), m.end(1))
        if not quoted:
            for span in request.state:
                text = span.text
                words = len(text.split())
                spoken = _SPEAKER.match(text)
                if spoken and words >= 6:
                    found.add("items", text[spoken.end() :], span.span_id, spoken.end(), len(text))
        if not found.items:  # posts: each block of the person's own sentences is one voice
            for span in request.state:
                text = span.text.strip()
                if "<" in text or len(text.split()) < 6 or not re.search(r"[.!?]$", text):
                    continue
                found.add("items", span.text, span.span_id, 0, len(span.text))
        return ExtractionResult(tuple((f.name, found.answer(f)) for f in request.fields))


_TERMS = re.compile(r"Terms that often signal it: (.*)\.$")
_ABOUT = re.compile(r"speak about (.*?)\?")


class ThemeMatch(RuleClassificationModel):
    """``theme_match``: a theme matches when the text holds one of its terms (or name words)."""

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """One boolean per theme question."""
        out = []
        for q in request.questions:
            terms_m = _TERMS.search(q.prompt)
            if terms_m:
                terms = [t.strip() for t in terms_m.group(1).split(",") if t.strip()]
            else:
                about = _ABOUT.search(q.prompt)
                name = about.group(1) if about else ""
                terms = [w[:6] + "*" for w in re.findall(r"[A-Za-z]{4,}", name)]
            out.append((q.name, _boolean(bool(analysis.term_hits(request.state, terms)))))
        return ClassificationResult(tuple(out))


# --- the decide step and the per-kind schemas ------------------------------------------------

SELLER_TYPES = frozenset({"product", "softwareapplication", "service", "offer", "aggregateoffer",
                          "individualproduct", "productmodel"})  # fmt: skip
SELLER_KEYS = frozenset({"product", "company", "brand", "vendor", "maker", "manufacturer",
                         "seller", "supplier", "provider", "og:site_name", "product:price:amount",
                         "price", "sku", "model"})  # fmt: skip
ARTICLE_TYPES = frozenset({"article", "newsarticle", "blogposting", "report", "review",
                           "scholarlyarticle", "dataset"})  # fmt: skip
SELL_WORDS = re.compile(
    r"\b(pricing|plans?|prices?|buy|order now|get a quote|subscribe|free trial|book a demo|"
    r"datasheet|what you get)\b",
    re.I,
)
_SELLS = re.compile(
    r"(?:^|[.!?]\s+)([A-Z][\w-]+(?: [A-Z][\w-]+){0,2}) (?:sells|offers|lists|charges|makes|"
    r"markets|rents|leases)\b"
)
"""A sentence whose subject sells something: "<Seller> sells ...", as a price list in prose."""
_BY_ANY = re.compile(
    r"\b[A-Z][\w-]+(?: [A-Z][\w-]+){0,3} (?:by|from) [A-Z][\w&.'-]+(?: [A-Z][\w&.'-]+){0,3}"
    r"\b(?: (?:Ltd|Inc|GmbH|AG|BV|SA|plc|LLC|Systems|Labs|Group))?"
)
"""A "<Product> by <Company>" line, the way a brochure or a datasheet names itself."""
CORPORATE = re.compile(
    r"\b([A-Z][\w&'-]+(?: [A-Z][\w&'-]+){0,3}),? (?:Inc\b\.?|Ltd\b\.?|LLC\b|GmbH\b|AG\b|BV\b|"
    r"S\.?A\.?\b|plc\b|PT\b|Pty\b|Corp\b\.?|Corporation\b|Limited\b|Oy\b|AB\b|SAS\b)"
)
"""A company's legal name: a capitalised name and its corporate form ("Siltgrove Inc.")."""
SELF_WORDS = re.compile(
    r"\b(datasheet|data sheet|spec(?:ification)? sheet|price ?list|brochure|plans? and pricing|"
    r"pricing|our plans|free plan|talk to (?:our )?sales|contact (?:our )?sales)\b",
    re.I,
)
"""Words a seller's own document uses about itself or its offer."""
REPORT_WORDS = re.compile(
    r"\b(market|survey|report|analysis|outlook|landscape|forecast|cagr|share of|respondents|"
    r"industry|segment|adoption|study|findings|methodology)\b",
    re.I,
)
NOTE_WORDS = re.compile(
    r"\b(priorit\w*|minutes|agenda|action items?|todo|to do|plan|roadmap|decided|we will|"
    r"our|internal|notes?|attendees|follow[- ]ups?)\b",
    re.I,
)
VOICE_WORDS = re.compile(
    r"\b(review(?:er|ed|s)?|rated|stars?|customer|user|respondent|interviewee|said|says|"
    r"verbatim|feedback|ticket|complain\w*)\b",
    re.I,
)
THREAD_WORDS = re.compile(r"\b(forums?|threads?|posted|posts)\b", re.I)
"""Words of a discussion page: a thread of dated posts is people's own words."""


def _preview(state: str) -> dict[str, Any]:
    """The decide preview's parts: its header lines and its columns (``document_preview``)."""
    out: dict[str, Any] = {"columns": {}}
    for line in state.splitlines():
        if line.startswith("- ") and ": " in line:
            name, _, values = line[2:].partition(": ")
            out["columns"][name] = [v.strip() for v in values.split(" | ") if v.strip()]
        elif ": " in line:
            k, _, v = line.partition(": ")
            out.setdefault(k, v)
    return out


def _decision(spec: QuestionSpec, chosen: str, sure: Decimal = SURE) -> ClassAnswer:
    chosen = chosen if chosen in spec.options else "other"
    rest = (1 - sure) / max(1, len(spec.options) - 1)
    weights = {o: sure if o == chosen else rest for o in spec.options}
    return ClassAnswer(chosen, distribution_ppm(weights))


MESSAGE = re.compile(
    r"^\s*\[?\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}[,\s]+\d{1,2}:\d{2}(?::\d{2})?\]?\s*(?:-\s*)?"
    r"[^:\n]{1,60}:\s+\S",
    re.M,
)
"""A message line of a chat export: its timestamp, its sender, then what they wrote."""


def text_lines(state: str) -> str:
    """The preview's text with each message's timestamp starting a line again."""
    text = _preview(state).get("text", "")
    return re.sub(r"\s(?=\[?\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}[,\s]+\d{1,2}:\d{2})", "\n", text)


def decide_record(state: str, options: Sequence[str]) -> tuple[str, Decimal]:
    """The kind a careful reader gives a record from its structural preview, and how sure.

    A table: customer voices when one column holds long free text beside a rating, a
    date, a product or a site; a price list when its values state money amounts beside
    a name. A text: the structured identity of an offer, or a money amount beside its
    seller's name, is a seller's page; a third party's report is market words over
    several names; customer voices are quoted or rated voices; an email, minutes, plans
    or priorities are the person's own note. A record that shows none of these is
    ``other``; one that shows two at once is decided at 0.6, inside the abstention band.
    """
    got = _preview(state)
    fmt = got.get("format", "text")
    cols: dict[str, list[str]] = got["columns"]
    if fmt in ("csv", "json") and cols:
        roles = {c: guess_role(c, v) for c, v in cols.items()}
        head = {k.split("=", 1)[0].strip() for k in got.get("header fields", "").split(";")}
        own = [k for k in head if set(name_words(k)) & (SELLER_COLUMN | {"product"})]
        longest = max((sum(len(x) for x in v) / len(v) for v in cols.values() if v), default=0)
        moneyed = [c for c, v in cols.items() if any(parsing.MONEY.search(x) for x in v)]
        context = {"rating", "date", "product", "site", "geography"} & set(roles.values())
        texty = "text" in roles.values() or (longest >= 40 and "rating" in context)
        if texty and context:
            return "customer_reviews", SURE
        if moneyed and own and not any(set(name_words(c)) & SELLER_COLUMN for c in cols):
            return "competitor_page", SURE  # one seller's own feed: it names itself above its plans
        if moneyed and any(r in ("product", "other", "id") for c, r in roles.items()
                           if c not in moneyed):  # fmt: skip
            return "pricing_table", SURE
        return "other", SURE
    text = got.get("text", "")
    fields = got.get("header fields", "")
    keys = {f.split("=", 1)[0].strip().lower() for f in fields.split(";") if f.strip()}
    values = {f.split("=", 1)[0].strip().lower(): f.split("=", 1)[-1].strip().lower()
              for f in fields.split(";") if "=" in f}  # fmt: skip
    types = {t.strip().lower() for t in got.get("structured data types", "").split(",") if t}
    rows = int(got.get("table rows", "0") or 0)
    money = len([m for m in parsing.MONEY.finditer(text) if _price_ok(text, m, rows >= 3)])
    sellers = {m.group(1) for m in _SELLS.finditer(text)}
    article = bool(types & ARTICLE_TYPES) or values.get("og:type") in ("article", "report")
    seller = (bool(types & SELLER_TYPES) or bool(keys & SELLER_KEYS)) and not article
    heads = got.get("headings", "")
    publication = bool(re.search(r"\(1[19]\)|\(2[12]\)|\(5[17]\)", text)) or (
        bool(re.search(r"\bclaims?\b", heads, re.I))
        and bool(re.search(r"\babstract\b", text, re.I))
    )
    authored = bool(keys & {"record_id", "owner", "invention", "attendees"}) or bool(
        re.search(r"\b(author|owner|attendees)\s*:", text, re.I)
    )
    if fmt != "html" and "author" in keys:
        authored = True  # an HTML page's author tag names its publisher, not a note's writer
    offers = bool(SELL_WORDS.search(heads))
    lead = (heads + " " + text[:300]).lower()
    report = (article or bool(REPORT_WORDS.search(lead))) and len(REPORT_WORDS.findall(text)) >= 2
    report = report and not authored and not publication
    chat = len(MESSAGE.findall(text_lines(state))) >= 2
    quoted = len(_QUOTE.findall(text))
    voices = quoted >= 3 or (len(VOICE_WORDS.findall(text)) >= 4 and quoted >= 1)
    if not voices and len(THREAD_WORDS.findall(text)) >= 2 and not money:
        voices = len(find_dates(text, "dmy")) >= 3  # a thread: several dated posts
    note = fmt == "email" or authored or len(NOTE_WORDS.findall(text)) >= 3
    if publication:
        return "other", SURE
    if chat and "conversation" in options and fmt != "html":
        return "conversation", SURE
    found: list[str] = []
    companies = {m.group(1).lower() for m in CORPORATE.finditer(text)}
    named = (
        bool(keys & {"og:title", "og:site_name", "application-name"})
        or bool(_BY_ANY.search(text[:600]))
        or len(companies) == 1  # one company's legal name: its own document
    )
    offers = offers or bool(SELF_WORDS.search(heads + " " + text[:400]))
    if seller or (named and (money or offers) and not article and not authored):
        found.append("competitor_page")
    if ((rows >= 3 and money >= 3) or (len(sellers) >= 2 and money >= 2)) and not seller:
        found.append("pricing_table")  # a table of prices, or a price list in prose
    if report and not seller:
        found.append("market_report")
    if voices and not seller:
        found.append("customer_reviews")
    if note and not found:
        found.append("internal_note")
    found = [k for k in found if k in options]
    if not found:
        return "other", SURE
    if len(found) == 1:
        return found[0], SURE
    if found[0] == "competitor_page":
        return "competitor_page", SURE
    return found[0], Decimal("0.6")


class DocKind(RuleClassificationModel):
    """``doc_kind``: the decide step, by :func:`decide_record` over the preview."""

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """The one ``kind`` question."""
        out = []
        for q in request.questions:
            kind, sure = decide_record(request.state, q.options)
            out.append((q.name, _decision(q, kind, sure)))
        return ClassificationResult(tuple(out))


SELLER_COLUMN = frozenset({"vendor", "seller", "company", "brand", "maker", "manufacturer",
                           "supplier", "provider", "competitor", "firm", "who"})  # fmt: skip
PRODUCT_COLUMN = frozenset({"product", "model", "plan", "tier", "item", "offering", "sku",
                            "package", "edition", "device", "name"})  # fmt: skip
PRICE_COLUMN = frozenset({"price", "cost", "fee", "list", "rate", "msrp", "rrp", "charge",
                          "subscription", "monthly", "annual"})  # fmt: skip


def _delimiter_of(texts: Sequence[str]) -> str | None:
    """The delimiter most of a table's row passages use (a title line's comma is not one)."""
    counts = {d: sum(1 for t in texts if t.count(d) >= (2 if d == "|" else 1))
              for d in ("|", "\t", ";", ",")}  # fmt: skip
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else None


def _cells_of(text: str, delim: str | None = None) -> list[tuple[str, int, int]]:
    """The cells of one row passage (an HTML row, a delimited line, a pipe row), with ranges.

    With the table's ``delim`` known, an empty cell is kept (as an empty range), so each
    cell stays under its column.
    """
    if re.search(r"</?t[dh]\b", text, re.I):  # an HTML row: the text between its cell tags
        out = []
        for m in re.finditer(r"[^<>]+(?=<|$)", text):
            s, e = m.start(), m.end()
            while s < e and text[s].isspace():
                s += 1
            while e > s and text[e - 1].isspace():
                e -= 1
            if e > s and (s == 0 or text[s - 1] == ">" or text[m.start() - 1] == ">"):
                out.append((text[s:e], s, e))
        return out
    if delim is not None and re.search(re.escape(delim), text):
        out = []
        at = 0
        for part in text.split(delim):
            s, e = at, at + len(part)
            while s < e and text[s] in ' \t"':
                s += 1
            while e > s and text[e - 1] in ' \t"':
                e -= 1
            out.append((text[s:e], s, e))
            at += len(part) + 1
        if delim == "|" and out and not out[0][0]:
            out = out[1:]  # a pipe row's leading bar
        if delim == "|" and out and not out[-1][0]:
            out = out[:-1]
        return out
    for delim in ("|", "\t", ";", ","):
        if text.count(delim) >= 1 and (delim != "|" or text.count("|") >= 2):
            out = []
            for m in re.finditer(r"[^" + re.escape(delim) + r"]+", text):
                s, e = m.start(), m.end()
                while s < e and text[s] in ' \t"':
                    s += 1
                while e > s and text[e - 1] in ' \t"':
                    e -= 1
                if e > s:
                    out.append((text[s:e], s, e))
            return out
    return [(text, 0, len(text))]


def _prose(text: str) -> bool:
    """Whether a passage is running sentences, not a delimited row."""
    return ("|" not in text and "\t" not in text and len(text.split()) >= 8
            and bool(re.search(r"[a-z][.!?](?:\s|$)", text)))  # fmt: skip


class PriceTable(RuleExtractionModel):
    """``price_table``: per row, the seller, the product and the price, by the header's words.

    The header passage names the columns; a row's cell under a seller-like column is its
    seller, under a product-like one its product, and a cell stating money is its price.
    With no header, the first named cell is the seller and the next the product.
    """

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """The row-wise fields the request names."""
        found = _Found()
        roles: dict[int, str] = {}
        rowlike = [sp.text for sp in request.state
                   if not sp.text.lstrip().startswith(("{", "<"))
                   and not _prose(sp.text)]  # fmt: skip
        delim = _delimiter_of(rowlike)
        for span in request.state:
            text = span.text
            if text.lstrip().startswith("{"):
                for key, group in (("sellers", SELLER_COLUMN), ("products", PRODUCT_COLUMN)):
                    for m in re.finditer(r'"([\w .-]+)"\s*:\s*"([^"]+)"', text):
                        if set(name_words(m.group(1))) & group:
                            found.add(key, m.group(2), span.span_id, m.start(2), m.end(2))
                            break
                for m in parsing.MONEY.finditer(text):
                    found.add("prices", text[m.start() : m.end()], span.span_id, m.start(),
                              m.end())  # fmt: skip
                    break
                continue
            if _prose(text):  # a price list in prose: each sentence's seller and its prices
                for m in _SELLS.finditer(text):
                    found.add("sellers", m.group(1), span.span_id, m.start(1), m.end(1))
                for m in parsing.MONEY.finditer(text):
                    if _price_ok(text, m, True):
                        found.add("prices", text[m.start() : m.end()].rstrip(), span.span_id,
                                  m.start(), m.start() + len(m.group(0).rstrip()))  # fmt: skip
                continue
            cells = _cells_of(text, None if "</t" in text else delim)
            if sum(1 for c in cells if c[0]) < 2:
                continue  # a title line or a blank row, not a row of the table
            if not any(parsing.MONEY.search(c[0]) for c in cells) and not roles and len(cells) > 1:
                for i, (name, _, _) in enumerate(cells):
                    words = set(name_words(name))
                    if words & SELLER_COLUMN:
                        roles[i] = "sellers"
                    elif words & PRICE_COLUMN:
                        roles[i] = "prices"
                    elif words & PRODUCT_COLUMN:
                        roles[i] = "products"
                continue
            priced = False
            named = [i for i, c in enumerate(cells) if re.search(r"[A-Za-z]", c[0])
                     and not parsing.MONEY.search(c[0])]  # fmt: skip
            for i, (value, s, e) in enumerate(cells):
                role = roles.get(i)
                if role is None and not roles:
                    role = ("sellers" if named and i == named[0] else
                            "products" if len(named) > 1 and i == named[1] else None)  # fmt: skip
                money = parsing.MONEY.search(value)
                if money is not None and not priced:
                    found.add("prices", value[money.start() : money.end()], span.span_id,
                              s + money.start(), s + money.end())  # fmt: skip
                    priced = True
                elif role in ("sellers", "products"):
                    found.add(role, value, span.span_id, s, e)
            for _m in ADDRESSED.finditer(text):
                found.add("addressed_to_agents", text, span.span_id, 0, len(text))
                break
        return ExtractionResult(tuple((f.name, found.answer(f)) for f in request.fields))


_ORG = re.compile(
    r"\b([A-Z][\w&'-]+(?: [A-Z][\w&'-]+){0,3} (?:Labs|Ltd|GmbH|Inc|AG|BV|SA|plc|Group|Systems|"
    r"Technologies|Agritech|Analytics|Robotics|Instruments))\b"
)
_FIGURE = re.compile(r"[^.;\n]*\b\d+(?:[.,]\d+)?\s?(?:%|percent|million|billion|units|farms?)\b"
                     r"[^.;\n]*")  # fmt: skip
_FUTURE = re.compile(r"\b(will|expected to|forecast|by 20\d\d|projected|likely to)\b", re.I)


class ReportFacts(RuleExtractionModel):
    """``report_facts``: companies, figures, forward claims and prices stated with a basis."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """The report fields the request names."""
        found = _Found()
        for span in request.state:
            text = span.text
            for m in _ORG.finditer(text):
                found.add("companies", m.group(1), span.span_id, m.start(1), m.end(1))
            clean, where = clean_map(text)
            for m in parsing.MONEY.finditer(clean):
                if _price_ok(clean, m, False):
                    s, e = _raw_range(where, m.start(), m.end(), clean)
                    found.add("prices", text[s:e], span.span_id, s, e)
            for s0, e0 in _sentences(text):
                sentence = text[s0:e0]
                if _FIGURE.search(sentence):
                    found.add("figures", sentence, span.span_id, s0, e0)
                if _FUTURE.search(sentence):
                    found.add("claims", sentence, span.span_id, s0, e0)
                if ADDRESSED.search(sentence):
                    found.add("addressed_to_agents", sentence, span.span_id, s0, e0)
            for m in _PUBLISHED_PHRASE.finditer(text):
                dates = find_dates(text[m.end() : m.end() + 40], "dmy")
                if dates and dates[0][1] == 0:
                    iso, ds, de = dates[0]
                    found.add("published", iso, span.span_id, m.end() + ds, m.end() + de)
        return ExtractionResult(tuple((f.name, found.answer(f)) for f in request.fields))


_STAMP = re.compile(r"\[?(\d{1,2})[/.](\d{1,2})[/.](\d{2}|\d{4})[,\s]+\d{1,2}:\d{2}")


def stamp_date(text: str) -> tuple[str, int, int] | None:
    """A chat line's timestamp as an ISO date, read day first (most locales write it so)."""
    m = _STAMP.search(text)
    if m is None:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if month > 12 >= day:
        day, month = month, day  # a second number over 12 is the day: written month first
    year += 2000 if year < 100 else 0
    iso = parse_date(f"{year:04d}-{month:02d}-{day:02d}")
    return (iso, m.start(1), m.end(3)) if iso else None


class ConversationFacts(RuleExtractionModel):
    """``conversation_facts``: each price a message quotes, and when that message was sent."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """The ``prices`` and ``published`` fields, from each message line."""
        found = _Found()
        first: tuple[str, Any, int, int] | None = None
        for span, at, line, lead in _lines_of(request.state):
            when = stamp_date(line)
            if when is not None and first is None:
                first = (when[0], span, at + lead + when[1], at + lead + when[2])
            for m in parsing.MONEY.finditer(line):
                if not _price_ok(line, m, False):
                    continue
                s, e = at + lead + m.start(), at + lead + m.end()
                found.add("prices", span.text[s:e], span.span_id, s, e)
                if when is not None and "published" not in found.items:
                    found.add("published", when[0], span.span_id, at + lead + when[1],
                              at + lead + when[2])  # fmt: skip
        if "published" not in found.items and first is not None:
            iso, span, s, e = first
            found.add("published", iso, span.span_id, s, e)
        return ExtractionResult(tuple((f.name, found.answer(f)) for f in request.fields))
