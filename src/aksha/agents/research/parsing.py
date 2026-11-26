"""The research agent's values over any input: passages, pages, tables and stated prices.

Nothing here recognises a layout. What a record holds (who sells, which passages state a
price, which column holds the review text) is the answer of the agent's extraction and
classification aliases (``reading.py``); this module holds the values those answers are
assembled into and the rules code applies to a quoted value, the same for any market:

1. :class:`Passage`, :class:`Page` and :class:`Table`: what the read tools return, each
   passage a ``(start, end)`` range of the record's raw text, so a tool asks the runtime to
   mint one citation span per passage.
2. :func:`price_fields` reads one quoted price: its currency (a code, a symbol before or
   after the amount, or ``%`` for a share of a value), the amount as written, the amount as
   a plain decimal (European and English separators both), its basis (``per unit``,
   ``/mo``, ``monthly``; empty when the quote states none) and its condition (``from``).
   :func:`basis_key` folds a basis so ``/unit``, ``per unit`` and ``a unit`` are one.
3. :data:`ROLES`: what a table column can hold, the vocabulary the ``table_roles`` alias
   answers in; never a list of column names.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aksha.runtime.api import FORMATS

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
    "seller",
    "offer",
)

ROLES = ("id", "text", "date", "product", "site", "geography", "rating", "rating_scale_max",
         "rating_unit", "other")  # fmt: skip
"""What a column of a table of customer voices can hold (the ``table_roles`` options)."""

__all__ = ["FORMATS", "PASSAGE_KINDS", "ROLES", "Page", "Passage", "Table"]


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
    """A record read as a page: its identity fields and its passages."""

    published: str | None
    title: str
    product: str | None
    brand: str | None
    url: str | None
    passages: tuple[Passage, ...]


@dataclass(frozen=True)
class Table:
    """Rows read from a record: the column roles and one passage per row's text."""

    columns: tuple[str, ...]
    roles: Mapping[str, str]
    passages: tuple[Passage, ...]
    header: Mapping[str, Any] = field(default_factory=dict)
    total: int = 0


# --- prices -------------------------------------------------------------------------------

SYMBOLS: Mapping[str, str] = {
    "€": "EUR", "$": "USD", "US$": "USD", "£": "GBP", "₹": "INR", "¥": "JPY", "₩": "KRW",
    "kr": "SEK", "zł": "PLN", "CHF": "CHF", "Rs": "INR", "Rs.": "INR", "R$": "BRL",
    "A$": "AUD", "C$": "CAD", "S$": "SGD",
}  # fmt: skip
"""Currency symbols to codes; an ambiguous symbol (``$``, ``kr``) takes its commonest code."""

ISO_CODES = (
    "EUR|USD|GBP|INR|JPY|CNY|AUD|CAD|CHF|SEK|NOK|DKK|PLN|BRL|MXN|SGD|HKD|NZD|ZAR|"
    "AED|SAR|KRW|TRY|IDR|THB|MYR|PHP|VND|CZK|HUF|ILS|KES|NGN|EGP|PKR|BDT|LKR|RON"
)
_CUR = rf"(?:\b(?:{ISO_CODES})\b|US\$|R\$|A\$|C\$|S\$|Rs\.?|\bkr\b|zł|[€$£₹¥₩])"
_AMT = r"\d{1,3}(?:[ ,.\u00a0\u202f]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_BASIS = (
    r"(?:\s*(?i:per|/|a|an|each)\s*(?P<per>[^.;,()\n|]{1,60}?)"
    r"|\s+(?P<per_word>(?i:monthly|yearly|annually|weekly|daily|one-off|one off|once)))"
    r"(?=$|[.;,()\n|]| billed| plus| incl| excl| \+| or | and |\s*$)"
)
MONEY = re.compile(
    rf"(?P<cond>\b(?i:from|starting at|starts at|only|just)\s+)?"
    rf"(?:(?P<cur>{_CUR})\s?(?P<amt>{_AMT})|(?P<amt2>{_AMT})\s?(?P<cur2>{_CUR})(?![A-Za-z]))"
    rf"(?:{_BASIS})?",
)
"""A money amount with its currency, an optional condition before and basis after."""
PERCENT = re.compile(
    r"(?P<amt>\d+(?:[.,]\d+)?)\s?%\s+(?:of|per|on)\s+(?P<per>[^.;,()\n|]{1,60}?)(?=$|[.;,()\n|])",
    re.I,
)


def amount_number(text: str, currency_after: bool = False) -> str:
    """The decimal value of an amount as written, separators read as people write them.

    ``1,499`` and ``1.499`` (a thousands dot after a code or symbol that follows it) are
    ``1499``; ``1.499,00`` is ``1499.00``.
    """
    t = re.sub(r"[\s\u00a0\u202f]", "", text)
    if "," in t and "." in t:
        dec = "," if t.rfind(",") > t.rfind(".") else "."
        thou = "." if dec == "," else ","
        return t.replace(thou, "").replace(dec, ".")
    for sep in (",", "."):
        if sep in t:
            parts = t.split(sep)
            if all(len(p) == 3 for p in parts[1:]) and (sep == "," or currency_after or
                                                        len(parts) > 2):  # fmt: skip
                return "".join(parts)
            if len(parts) == 2 and len(parts[1]) <= 2:
                return parts[0] + "." + parts[1]
            return "".join(parts)
    return t


def price_fields(text: str) -> dict[str, Any] | None:
    """The first price stated in ``text`` (module doc, item 2); None when there is none."""
    stripped = " ".join(text.split())
    m = MONEY.search(stripped)
    p = PERCENT.search(stripped)
    if p is not None and (m is None or p.start() < m.start()):
        return {"currency": "%", "amount": p["amt"], "number": amount_number(p["amt"]),
                "per": p["per"].strip(), "condition": ""}  # fmt: skip
    if m is None:
        return None
    cur = m["cur"] or m["cur2"]
    amt = m["amt"] or m["amt2"]
    code = SYMBOLS.get(cur, SYMBOLS.get(cur.lower(), cur.upper()))
    per = (m["per"] or m["per_word"] or "").strip()
    return {
        "currency": code,
        "amount": amt,
        "number": amount_number(amt, currency_after=m["amt2"] is not None),
        "per": per,
        "condition": (m["cond"] or "").strip().lower(),
    }


_BASIS_WORDS = {"mo": "month", "month": "month", "monthly": "month", "yr": "year",
                "year": "year", "yearly": "year", "annually": "year", "annum": "year",
                "wk": "week", "weekly": "week", "day": "day", "daily": "day"}  # fmt: skip


def basis_key(per: str) -> str:
    """``per`` folded so equal bases compare equal: ``/mo``, ``per month``, ``monthly``."""
    words = re.findall(r"[a-z0-9]+", per.lower())
    words = [w for w in words if w not in ("per", "a", "an", "each", "the")]
    words = [_BASIS_WORDS.get(w, w[:-1] if w.endswith("s") and len(w) > 3 else w) for w in words]
    return " ".join(words)
