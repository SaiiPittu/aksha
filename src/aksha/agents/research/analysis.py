"""The research agent's rules, as pure functions over what its tools returned.

1. Entity resolution (:func:`resolve`): pages are merged into one company only when one
   page's own text names another page's product or company in a relationship sentence
   ("is the new name for", "joined", "acquired"). Names that merely look alike are never
   merged. The canonical name is the company of the earliest-published page; every other
   company and product name is an alias, and the sentence is the merge's evidence.
2. Prices (:func:`price_conflict`): a company's one-off unit prices that differ across its
   pages are a conflict, kept open with each page and date, never averaged or picked.
3. Reviews (:func:`review_sample`): a row whose product and normalised text repeat an
   earlier row is a cross-posted duplicate, counted once. Themes are keyword rules over
   the unique rows; a theme's size is reported as "N of M reviews in this sample", with
   its dates, geographies and products, never as a share of a market.
4. Claim labels (:func:`claim_label`): marketing copy ("up to", "claimed", a hero line)
   is a vendor claim; a specification row that states its condition is a fact as stated.

Nothing here reads a file or a clock; the inputs are tool results.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

THEMES: Mapping[str, str] = {
    "battery": r"\bbatter(?:y|ies)\b(?! powered)",
    "stillness": r"\b(?:sit\w*|sat|seated|still|do not move|barely move)\b",
    "setup": r"\b(?:cabl\w*|electrician|sockets?|wiring|install\w*|setup)\b",
    "privacy": r"\b(?:privacy|works council|image)\b",
}
"""Each theme's keyword rule, matched case-insensitively against a review's text."""

NEEDS: Mapping[str, str] = {
    "battery": "Battery life that holds at a real office reporting rate",
    "stillness": "Counts that stay right while people sit still",
    "setup": "An install with no new cabling, sockets or electrician",
    "privacy": "Written proof that no image leaves the sensor",
}
"""The customer need each theme is evidence for, in the buyer's terms."""

_RELATION_WORDS = re.compile(
    r"\b(is the new name for|joined|acquired|was acquired by|merged with|now part of|renamed)\b",
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
    for members in groups.values():
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
                key=slug(canonical),
                canonical=canonical,
                aliases=tuple(names),
                pages=tuple(ordered),
                merges=tuple(m for m in merges if m[0].record in mine),
            )
        )
    return tuple(sorted(out, key=lambda c: c.key))


def price_conflict(points: Iterable[Mapping[str, Any]]) -> bool:
    """Whether a company's one-off unit prices (``currency``, ``value``) differ (rule 2)."""
    unit = {(p["currency"], p["value"]) for p in points if p.get("per") == "unit"}
    return len(unit) > 1


def claim_label(kind: str, text: str) -> str:
    """``vendor_claim`` for marketing copy, ``fact`` for a stated specification (rule 4)."""
    if kind == "spec":
        return "fact"
    if kind == "claim" or re.search(r"\b(up to|claimed|claims)\b", text, re.I):
        return "vendor_claim"
    return "fact"


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


def review_sample(rows: Sequence[Mapping[str, Any]]) -> ReviewSample:
    """Duplicates and themes over review rows (rule 3); each row has ``fields`` and ``text``."""
    seen: dict[tuple[str, str], Mapping[str, Any]] = {}
    unique: list[Mapping[str, Any]] = []
    dups: list[tuple[str, str]] = []
    ordered = sorted(
        rows, key=lambda r: (str(r["fields"].get("date", "")), r["fields"]["review_id"])
    )
    for r in ordered:
        sig = (str(r["fields"].get("product", "")), _norm(str(r["text"])))
        if sig in seen:
            dups.append((str(r["fields"]["review_id"]), str(seen[sig]["fields"]["review_id"])))
            continue
        seen[sig] = r
        unique.append(r)
    by_id = sorted(unique, key=lambda r: str(r["fields"]["review_id"]))
    themes = {
        name: tuple(r for r in by_id if re.search(rule, str(r["text"]), re.I))
        for name, rule in THEMES.items()
    }
    return ReviewSample(
        rows=tuple(rows),
        unique=tuple(by_id),
        duplicates=tuple(sorted(dups)),
        themes=themes,
    )


def tally(rows: Iterable[Mapping[str, Any]], field_name: str) -> dict[str, int]:
    """How many rows hold each value of ``fields[field_name]``, by value."""
    out: dict[str, int] = {}
    for r in rows:
        v = str(r["fields"].get(field_name, ""))
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))
