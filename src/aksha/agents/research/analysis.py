"""The research agent's rules, as pure functions over what its tools returned.

None of them knows a market. Each is a rule a careful analyst applies to any inputs:

1. Entity resolution (:func:`resolve`): pages are merged into one company only when one
   page's own text names another page's product or company in a relationship sentence
   ("is the new name for", "joined", "acquired", "now part of"). Names that merely look
   alike are never merged. The canonical name is the company of the earliest-published
   page; every other company and product name is an alias, and the sentence is the merge's
   evidence. Companies come in the order their first page was discovered (the order of the
   pages given), never by name, so a renamed world is worked through in the same order;
   the key leads with that position (``c01-...``) because the runtime fans out by sorted
   key.
2. Prices (:func:`price_conflict`): when each of two or more pages of one company states
   exactly one price on the same basis (``per unit``, ``per order``) and those prices
   differ, that is a conflict, kept open with each page and date, never averaged or picked.
   Several prices on one page are tiers, not a conflict.
3. Terms (:func:`term_hits`): a theme or a comparison dimension is matched by the terms the
   request gives it. A term matches as a whole word or phrase, case folded; ``batter*`` is
   a prefix; ``not battery powered`` excludes the text it covers from the other terms.
4. Reviews (:func:`review_sample`): a row whose product and normalised text repeat an
   earlier row is a cross-posted duplicate, counted once. A theme's size is reported as
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
from typing import Any

_RELATION_WORDS = re.compile(
    r"\b(is the new name for|joined|acquired|was acquired by|merged with|now part of|renamed|"
    r"is now|rebranded as|formerly)\b",
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


@dataclass(frozen=True)
class Company:
    """One resolved company: its key, canonical name, aliases, pages and merge evidence."""

    key: str
    canonical: str
    aliases: tuple[str, ...]
    pages: tuple[PageIdentity, ...]
    merges: tuple[tuple[PageIdentity, PageIdentity, Mapping[str, Any]], ...] = ()


def resolve(pages: Sequence[PageIdentity]) -> tuple[Company, ...]:
    """Group ``pages`` into companies by relationship sentences only (module doc, rule 1)."""
    parent = {p.record: p.record for p in pages}

    def find(r: str) -> str:
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
                if other.record == page.record:
                    continue
                named = [n for n in (other.product, other.brand) if n and n in text]
                if not named:
                    continue
                a, b = find(page.record), find(other.record)
                if a != b:
                    parent[b] = a
                    merges.append((page, other, rel))
    groups: dict[str, list[PageIdentity]] = {}
    for p in pages:
        groups.setdefault(find(p.record), []).append(p)
    out: list[Company] = []
    for rank, members in enumerate(groups.values(), start=1):
        ordered = sorted(members, key=lambda p: (p.published or "9999", p.record))
        canonical = ordered[0].brand or ordered[0].product
        names: list[str] = []
        for p in ordered:
            for n in (p.brand, p.product):
                if n and n != canonical and n not in names:
                    names.append(n)
        mine = {p.record for p in members}
        out.append(
            Company(
                key=f"c{rank:02d}-{slug(canonical)}",
                canonical=canonical,
                aliases=tuple(names),
                pages=tuple(ordered),
                merges=tuple(m for m in merges if m[0].record in mine),
            )
        )
    return tuple(out)


def price_conflict(points: Iterable[Mapping[str, Any]]) -> list[str]:
    """The price bases on which a company's pages disagree (module doc, rule 2).

    Each point has ``record``, ``per``, ``currency`` and ``value``. A basis conflicts when
    at least two pages state exactly one price on it each and those prices differ.
    """
    by_basis: dict[str, dict[str, set[tuple[str, str]]]] = {}
    for p in points:
        per = " ".join(str(p.get("per", "")).lower().split())
        page = str(p.get("record", ""))
        value = (str(p.get("currency")), str(p.get("value")))
        by_basis.setdefault(per, {}).setdefault(page, set()).add(value)
    out = []
    for per, pages in sorted(by_basis.items()):
        single = [next(iter(v)) for v in pages.values() if len(v) == 1]
        if len(single) >= 2 and len(set(single)) > 1:
            out.append(per)
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
    seen: dict[tuple[str, str], Mapping[str, Any]] = {}
    unique: list[Mapping[str, Any]] = []
    dups: list[tuple[str, str]] = []
    ordered = sorted(rows, key=lambda r: (str(r["fields"].get("date", "")), str(r["fields"]["id"])))
    for r in ordered:
        sig = (str(r["fields"].get("product", "")), _norm(str(r["text"])))
        if sig in seen:
            dups.append((str(r["fields"]["id"]), str(seen[sig]["fields"]["id"])))
            continue
        seen[sig] = r
        unique.append(r)
    by_id = sorted(unique, key=lambda r: str(r["fields"]["id"]))
    matched = {
        name: tuple(r for r in by_id if term_hits(str(r["text"]), terms))
        for name, terms in themes.items()
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
