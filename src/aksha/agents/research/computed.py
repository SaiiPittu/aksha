"""The counts the research agent's checks assert, computed from the content being saved.

A model is asked to find, read and judge, never to count: every number a completion check
compares is written here, at save, over whatever the model supplied (ADR-P04 K1). Each
function is pure, takes a plain copy of the content and leaves a field it cannot compute
(a malformed list, say) as it was, so the schema and the checks report it.

- ``competitors``: each company's ``key`` (kept when the model supplied a valid one, else a
  slug of its canonical name, else of its first page's record), ``resolved`` the number of
  companies, ``pages_found`` the number of pages listed under them.
- ``matrix``: each row's ``key``, when the row has none, the same slug of the company it
  names, so a row matches its company without the model writing either key.
- ``needs``: ``sample.rows`` is the unique reviews plus the cross-posted duplicates (or,
  when only ``rows`` is given, ``unique`` is ``rows`` less the duplicates), and each need's
  ``phrase`` is "N of M reviews in this sample", N the reviews the need cites and M the
  unique reviews; a need's ``count`` and ``of``, where it carries them, are the same N and M.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .analysis import slug


def _count(value: Any) -> int | None:
    """``value`` when it is a whole number of things (not a bool, not negative)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def phrase(n: int, of: int) -> str:
    """How a theme's size is said: a count in this sample, never a share of a market."""
    return f"{n} of {of} reviews in this sample"


def cited_reviews(need: Mapping[str, Any]) -> list[Any] | None:
    """The reviews a need rests on (``reviews``, else ``evidence``), or None if not a list.

    Checks read saved content, which is frozen (tuples), so either sequence is taken.
    """
    found = need.get("reviews") or need.get("evidence", [])
    return list(found) if isinstance(found, list | tuple) else None


def valid_key(value: Any) -> bool:
    """A key an instance ID can carry: a non-empty string with no ``/``, ``[`` or ``]``."""
    return isinstance(value, str) and bool(value) and not any(c in value for c in "/[]")


def _name(value: Any) -> str | None:
    """A name given as a string or as a cited ``{value, ...}``, or None."""
    if isinstance(value, Mapping):
        value = value.get("value")
    return value.strip() if isinstance(value, str) and value.strip() else None


def company_key(company: Mapping[str, Any]) -> str | None:
    """The key code gives a company: a slug of its canonical name, else of its first page."""
    name = _name(company.get("canonical")) or _name(company.get("name"))
    if name is None:
        pages = company.get("pages")
        first = pages[0] if isinstance(pages, list) and pages else None
        name = _name(first.get("record")) if isinstance(first, Mapping) else None
    return None if name is None else slug(name)


def _keyed(items: list[Any], key_of: Any) -> None:
    """Give every object in ``items`` without a valid ``key`` one from ``key_of``, unique."""
    taken = {i["key"] for i in items if isinstance(i, Mapping) and valid_key(i.get("key"))}
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict) or valid_key(item.get("key")):
            continue
        base = key_of(item) or f"company-{n}"
        key, i = base, 2
        while key in taken:
            key, i = f"{base}-{i}", i + 1
        item["key"] = key
        taken.add(key)


def competitors(content: dict[str, Any]) -> dict[str, Any]:
    """Each company's ``key``, and ``resolved`` and ``pages_found`` counted from them."""
    companies = content.get("companies")
    if not isinstance(companies, list):
        return content
    _keyed(companies, company_key)
    content["resolved"] = len(companies)
    content["pages_found"] = sum(
        len(c["pages"])
        for c in companies
        if isinstance(c, Mapping) and isinstance(c.get("pages"), list)
    )
    return content


def matrix(content: dict[str, Any]) -> dict[str, Any]:
    """Each row's ``key``, when it has none, from the company the row names."""
    rows = content.get("rows")
    if isinstance(rows, list):
        _keyed(rows, lambda r: company_key({"canonical": r.get("company"), "name": r.get("name")}))
    return content


def needs(content: dict[str, Any]) -> dict[str, Any]:
    """The sample's ``rows`` (or ``unique``) and each need's "N of M" phrase, computed."""
    sample = content.get("sample")
    if not isinstance(sample, dict):
        return content
    duplicates = sample.get("duplicates", [])
    copies = len(duplicates) if isinstance(duplicates, list) else None
    unique, rows = _count(sample.get("unique")), _count(sample.get("rows"))
    if copies is not None and unique is not None:
        sample["rows"] = unique + copies
    elif copies is not None and rows is not None and rows >= copies:
        unique = sample["unique"] = rows - copies
    listed = content.get("needs")
    if unique is None or not isinstance(listed, list):
        return content
    for need in listed:
        if not isinstance(need, dict) or (reviews := cited_reviews(need)) is None:
            continue
        need["phrase"] = phrase(len(reviews), unique)
        if "count" in need:
            need["count"] = len(reviews)
        if "of" in need:
            need["of"] = unique
    return content
