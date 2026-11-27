"""The research agent's rules, as pure functions over what its tools returned.

None of them knows a market. Each is a rule a careful analyst applies to any inputs:

1. Entity resolution (:func:`resolve`): pages are merged into one company only when one
   page's own text names another page's product or company in a relationship sentence
   ("is the new name for", "joined", "acquired", "now part of", "is a trading name of"),
   or when another record (a newsletter, a memo) states that two pages' names are one
   company. Names that merely look alike are never merged. The canonical name is the
   company of the earliest-published page; every other company and product name is an
   alias, and the sentence is the merge's evidence. Companies come in the order their
   first page was discovered (the order of the pages given), never by name, so a renamed
   world is worked through in the same order; the key leads with that position
   (``c01-...``) because the runtime fans out by sorted key.
2. Prices (:func:`price_conflict`): when each of two or more pages of one company states
   exactly one price on the same basis (``per unit``, ``per order``) and those prices
   differ, that is a conflict, kept open with each page and date, never averaged or picked.
   Several prices on one page are tiers, not a conflict.
3. Terms (:func:`term_hits`): a theme or a comparison dimension is matched by the terms the
   request gives it. A term matches as a whole word or phrase, case folded; ``batter*`` is
   a prefix; ``not battery powered`` excludes the text it covers from the other terms.
4. Reviews (:func:`review_sample`): a row whose normalised text repeats an earlier row
   (its own product's name masked, so a copy under another listing or after a rename
   still matches), or ends with an earlier row's text after a lead-in ("Cross-posting
   from ...:"), is a cross-posted duplicate, counted once. A theme's size is reported as
   "N of M reviews in this sample", with its dates, geographies and products, never as a
   share of a market.
5. Claim labels (:func:`claim_label`): marketing copy ("up to", "claimed", a hero line)
   is a vendor claim; a specification row that states its condition is a fact as stated.

Nothing here reads a file or a clock; the inputs are tool results.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .parsing import amount_number, basis_key


def _decimal(text: str) -> bool:
    try:
        Decimal(text)
    except (InvalidOperation, ValueError):
        return False
    return True


_RELATION_WORDS = re.compile(
    r"\b(is the new name for|joined|acquired|was acquired by|merged with|now part of|renamed|"
    r"is now|rebranded as|formerly|trading name of|trades as|now called)\b",
    re.I,
)


def slug(name: str) -> str:
    """A key an instance ID can carry: lower case, runs of other characters as one ``-``."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unnamed"


@dataclass(frozen=True)
class PageIdentity:
    """What a fetched page says about who it is: its record, date, names and relations."""

    record: str
    version: str
    published: str
    product: str
    brand: str
    url: str | None = None
    relations: tuple[Mapping[str, Any], ...] = ()  # {"text", "span"} of relationship lines
    names: Mapping[str, Mapping[str, str]] = field(default_factory=dict)  # kind -> {text, span}
    section: int | None = None  # one section of a record holding several sellers' sections

    @property
    def page(self) -> tuple[str, int | None]:
        """The page's identity: its record, and its section when it is one."""
        return (self.record, self.section)


@dataclass(frozen=True)
class Company:
    """One resolved company: its key, canonical name, aliases, pages and merge evidence."""

    key: str
    canonical: str
    aliases: tuple[str, ...]
    pages: tuple[PageIdentity, ...]
    merges: tuple[tuple[PageIdentity, PageIdentity, Mapping[str, Any]], ...] = ()


def resolve(
    pages: Sequence[PageIdentity], statements: Sequence[Sequence[Mapping[str, Any]]] = ()
) -> tuple[Company, ...]:
    """Group ``pages`` into companies by relationship sentences only (module doc, rule 1).

    ``statements`` are other records' relationship sentences (a newsletter announcing a
    rename), each ``{"text", "span", "record", "version"}``: one that names two pages'
    names merges those pages, with the statement as the evidence.
    """
    parent = {p.page: p.page for p in pages}

    def find(r: tuple[str, int | None]) -> tuple[str, int | None]:
        while parent[r] != r:
            r = parent[r]
        return r

    merges: list[tuple[PageIdentity, PageIdentity, Mapping[str, Any]]] = []
    for page in pages:
        for rel in page.relations:
            text = str(rel["text"])
            if not _RELATION_WORDS.search(text):
                continue
            for other in pages:
                if other.page == page.page:
                    continue
                named = [n for n in (other.product, other.brand) if n and n in text]
                if not named:
                    continue
                a, b = find(page.page), find(other.page)
                if a != b:
                    parent[b] = a
                    merges.append((page, other, rel))
    for said in statements:
        for rel in said:
            text = str(rel["text"])
            if not _RELATION_WORDS.search(text):
                continue
            hits = [q for q in pages if any(n and n in text for n in (q.product, q.brand))]
            for other in hits[1:]:
                a, b = find(hits[0].page), find(other.page)
                if a != b:
                    parent[b] = a
                    merges.append((hits[0], other, rel))  # the quote is in rel["record"]
    groups: dict[tuple[str, int | None], list[PageIdentity]] = {}
    for p in pages:
        groups.setdefault(find(p.page), []).append(p)
    out: list[Company] = []
    for rank, members in enumerate(groups.values(), start=1):
        ordered = sorted(members, key=lambda p: (p.published or "9999", p.record, p.section or 0))
        canonical = ordered[0].brand or ordered[0].product
        names: list[str] = []
        for p in ordered:
            for n in (p.brand, p.product):
                if n and n != canonical and n not in names:
                    names.append(n)
        mine = {p.page for p in members}
        out.append(
            Company(
                key=f"c{rank:02d}-{slug(canonical)}",
                canonical=canonical,
                aliases=tuple(names),
                pages=tuple(ordered),
                merges=tuple(m for m in merges if m[0].page in mine),
            )
        )
    return tuple(out)


def price_conflict(points: Iterable[Mapping[str, Any]]) -> list[str]:
    """The price bases on which a company's pages disagree (module doc, rule 2).

    Each point has ``record``, ``per``, ``currency`` and ``value`` (and ``number``, the
    value as a plain decimal, when known). A basis conflicts when at least two pages state
    exactly one price on it each and those prices differ. Bases compare folded
    (``/unit`` is ``per unit``) and values by their decimal, so ``1,499`` is ``1499``.
    """
    by_basis: dict[str, dict[str, set[tuple[str, str]]]] = {}
    shown: dict[str, str] = {}
    for p in points:
        written = " ".join(str(p.get("per", "")).lower().split())
        per = basis_key(written)
        shown.setdefault(per, written)
        page = str(p.get("record", ""))
        number = str(p.get("number") or amount_number(str(p.get("value"))))
        if _decimal(number):
            number = format(Decimal(number).normalize(), "f")
        value = (str(p.get("currency")), number)
        by_basis.setdefault(per, {}).setdefault(page, set()).add(value)
    out = []
    for per, pages in sorted(by_basis.items()):
        single = [next(iter(v)) for v in pages.values() if len(v) == 1]
        if len(single) >= 2 and len(set(single)) > 1:
            out.append(shown[per])
    return out


def claim_label(kind: str, text: str) -> str:
    """``vendor_claim`` for marketing copy, ``fact`` for a stated specification (rule 5)."""
    if kind == "spec":
        return "fact"
    if kind == "claim" or re.search(r"\b(up to|claimed|claims)\b", text, re.I):
        return "vendor_claim"
    return "fact"


def _term_pattern(term: str) -> re.Pattern[str]:
    words = term.strip().lower()
    prefix = words.endswith("*")
    body = re.escape(words.rstrip("*")).replace(r"\ ", r"\s+")
    return re.compile(rf"\b{body}" + (r"" if prefix else r"\b"), re.I)


def term_hits(text: str, terms: Sequence[str]) -> list[str]:
    """The terms of ``terms`` that ``text`` holds (module doc, rule 3), in term order."""
    include = [t for t in terms if not t.lower().startswith("not ")]
    exclude = [t[4:] for t in terms if t.lower().startswith("not ")]
    covered: list[tuple[int, int]] = []
    for t in exclude:
        covered.extend(m.span() for m in _term_pattern(t).finditer(text))
    hits = []
    for t in include:
        for m in _term_pattern(t).finditer(text):
            if not any(a <= m.start() < b for a, b in covered):
                hits.append(t)
                break
    return hits


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


REPOST_MIN = 40
"""How long a text must be before a longer text ending in it is read as its repost."""
COPY_MIN_WORDS = 5
"""How many words the same text needs under another product before it is read as a copy.

Two customers of two products may both write "Works well."; that is two reviews."""


def _signature(row: Mapping[str, Any]) -> str:
    """A review's words for finding copies: folded, its own product's name masked.

    A copy is the same words wherever it was posted: on another site, under another
    listing of the same product, or after a rename that changed the product's name in it.
    """
    text = _norm(str(row["text"]))
    product = _norm(str(row["fields"].get("product", "")))
    if len(product) >= 3:
        text = text.replace(product, "\u2063")
    return text


@dataclass(frozen=True)
class ReviewSample:
    """The review rows read as a sample: unique rows, duplicates and each theme's rows."""

    rows: tuple[Mapping[str, Any], ...]
    unique: tuple[Mapping[str, Any], ...]
    duplicates: tuple[tuple[str, str], ...]  # (row, the earlier row it repeats)
    themes: Mapping[str, tuple[Mapping[str, Any], ...]]

    def phrase(self, theme: str) -> str:
        """``"7 of 40 reviews in this sample"``: a count of this sample, never a share."""
        return f"{len(self.themes[theme])} of {len(self.unique)} reviews in this sample"


def review_sample(
    rows: Sequence[Mapping[str, Any]], themes: Mapping[str, Sequence[str]]
) -> ReviewSample:
    """Duplicates and themes over review rows (rules 3 and 4).

    Each row has ``fields`` (``id``, and ``date`` and ``product`` when the export has
    them) and ``text``; ``themes`` maps each theme to its terms.
    """
    seen: dict[str, Mapping[str, Any]] = {}
    unique: list[Mapping[str, Any]] = []
    dups: list[tuple[str, str]] = []
    ordered = sorted(rows, key=lambda r: (str(r["fields"].get("date", "")), str(r["fields"]["id"])))
    for r in ordered:
        sig = _signature(r)
        if len(sig.split()) < COPY_MIN_WORDS:  # a short common phrase: a copy only per product
            sig = f"{_norm(str(r['fields'].get('product', '')))}\x00{sig}"
        earlier = seen.get(sig) or next(
            (o for k, o in seen.items() if len(k) >= REPOST_MIN and sig.endswith(" " + k)), None
        )
        if earlier is not None:
            dups.append((str(r["fields"]["id"]), str(earlier["fields"]["id"])))
            continue
        seen[sig] = r
        unique.append(r)
    by_id = sorted(unique, key=lambda r: str(r["fields"]["id"]))

    def speaks(r: Mapping[str, Any], name: str, terms: Sequence[str]) -> bool:
        said = r["fields"].get("themes")  # the theme_match alias's answer, when it was asked
        if isinstance(said, list | tuple):
            return name in said
        return bool(term_hits(str(r["text"]), terms))

    matched = {
        name: tuple(r for r in by_id if speaks(r, name, terms)) for name, terms in themes.items()
    }
    return ReviewSample(
        rows=tuple(rows),
        unique=tuple(by_id),
        duplicates=tuple(sorted(dups)),
        themes=matched,
    )


def tally(rows: Iterable[Mapping[str, Any]], field_name: str) -> dict[str, int]:
    """How many rows hold each value of ``fields[field_name]``, by value (absent: skipped)."""
    out: dict[str, int] = {}
    for r in rows:
        if field_name not in r["fields"]:
            continue
        v = str(r["fields"][field_name])
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))
