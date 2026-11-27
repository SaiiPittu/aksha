"""The counts the research agent's checks assert, computed from the content being saved.

A model is asked to find, read and judge, never to count: every number a completion check
compares is written here, at save, over whatever the model supplied (ADR-P04 K1). Each
function is pure, takes a plain copy of the content and leaves a field it cannot compute
(a malformed list, say) as it was, so the schema and the checks report it.

- ``competitors``: ``resolved`` is the number of companies, ``pages_found`` the number of
  pages listed under them.
- ``needs``: ``sample.rows`` is the unique reviews plus the cross-posted duplicates (or,
  when only ``rows`` is given, ``unique`` is ``rows`` less the duplicates), and each need's
  ``phrase`` is "N of M reviews in this sample", N the reviews the need cites and M the
  unique reviews; a need's ``count`` and ``of``, where it carries them, are the same N and M.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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


def competitors(content: dict[str, Any]) -> dict[str, Any]:
    """``resolved`` and ``pages_found`` counted from the companies listed."""
    companies = content.get("companies")
    if not isinstance(companies, list):
        return content
    content["resolved"] = len(companies)
    content["pages_found"] = sum(
        len(c["pages"])
        for c in companies
        if isinstance(c, Mapping) and isinstance(c.get("pages"), list)
    )
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
