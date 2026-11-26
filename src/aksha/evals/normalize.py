"""Normalisation before comparison: names, enum spellings, whitespace, money and spans.

Matching normalises dates, currencies, casing and whitespace before comparing (eval design
section 3.1), so a grader matches meaning, not shape.
The perturbed oracles (``selftest``) exist to catch a grader that forgot to.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_SPACE = re.compile(r"\s+")


def squash(text: str) -> str:
    """Whitespace runs folded to one space, ends stripped."""
    return _SPACE.sub(" ", text).strip()


def name_key(text: str) -> str:
    """A name for matching: case-folded, punctuation dropped, whitespace folded."""
    return squash(re.sub(r"[^\w\s]", " ", text.casefold()))


def enum_key(value: object) -> str:
    """An enum value for matching: ``Not Found``, ``not-found`` and ``NOT_FOUND`` agree."""
    return re.sub(r"[\s-]+", "_", str(value).strip().casefold())


def cell_state(value: object) -> str:
    """A matrix cell's state: the enum before any parenthetical qualifier, normalised.

    The key writes some cells as ``"disclosed (SYN-PUB-0001 only; ...)"``; the state is the
    first word, and the qualifier is a note the grader does not compare.
    """
    if isinstance(value, Mapping):
        value = value.get("state", "")
    return enum_key(str(value).split("(", 1)[0])


_CURRENCY = {"€": "EUR", "$": "USD", "£": "GBP", "eur": "EUR", "usd": "USD", "gbp": "GBP"}


def currency(value: object) -> str | None:
    """A currency code, upper case; symbols and lower case accepted."""
    if value is None:
        return None
    text = str(value).strip()
    return _CURRENCY.get(text.casefold(), _CURRENCY.get(text, text.upper()))


def amount(value: object) -> int | None:
    """An integer amount from an int, a decimal string or ``"EUR 249"``; None otherwise."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    m = re.search(r"-?\d+(?:\.\d+)?", str(value))
    if m is None:
        return None
    number = float(m.group(0))
    return int(number) if number.is_integer() else None


def price_of(entry: Mapping[str, Any]) -> tuple[int | None, str | None, str | None]:
    """(amount, currency, per) of a price entry, with ``"EUR 249"`` style amounts split."""
    raw = entry.get("amount")
    cur = entry.get("currency")
    if cur is None and isinstance(raw, str):
        m = re.match(r"\s*([A-Za-z€$£]{1,3})\s*\d", raw)
        cur = m.group(1) if m else None
    per = entry.get("per")
    return amount(raw), currency(cur), enum_key(per) if per is not None else None


def is_unknown(value: object) -> bool:
    """Whether a field value is an abstention: None or an ``unknown`` style label."""
    return value is None or enum_key(value) in {
        "unknown",
        "not_found",
        "needs_counsel_confirmation",
        "missing",
        "n_a",
    }
