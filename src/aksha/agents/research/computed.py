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
- ``needs``: when the sample lists the reviews read (``sample.reviews``, one ID per review
  row, cross-posted copies included), ``rows`` is the number of distinct IDs listed and
  ``unique`` is ``rows`` less the duplicates, whatever counts the model gave; otherwise
  ``sample.rows`` is the unique reviews plus the cross-posted duplicates (or, when only
  ``rows`` is given, ``unique`` is ``rows`` less the duplicates). A need that names its
  theme as ``theme_id`` or ``id`` and not ``theme`` has it copied to ``theme``, and a theme
  that names a scope theme by its name (case folded) is written as its ID. Each need's
  ``phrase`` is "N of M reviews in this sample", N the reviews the need cites and M the
  unique reviews; a need's ``count`` and ``of``, where it carries them, are the same N and M.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
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


def review_ids(sample: Mapping[str, Any]) -> list[str] | None:
    """The distinct review IDs ``sample.reviews`` lists, in order, or None if not a list.

    An entry is an ID, or an object carrying it as ``review`` or ``id``.
    """
    listed = sample.get("reviews")
    if not isinstance(listed, list | tuple):
        return None
    seen: dict[str, None] = {}
    for item in listed:
        if isinstance(item, Mapping):
            item = item.get("review", item.get("id"))
        if isinstance(item, str | int) and not isinstance(item, bool) and str(item).strip():
            seen.setdefault(str(item).strip(), None)
    return list(seen)


def need_theme(need: Mapping[str, Any]) -> Any:
    """The theme a need names: ``theme``, else ``theme_id``, else ``id``."""
    for field in ("theme", "theme_id", "id"):
        if need.get(field) not in (None, ""):
            return need[field]
    return None


def _folded(text: Any) -> str:
    return " ".join(str(text).split()).casefold()


def theme_resolver(scope: Mapping[str, Any] | None) -> Callable[[Any], str | None]:
    """A function from a need's theme to the scope theme ID it names, or None.

    A theme names a scope theme by its ID, or by its ID or name case folded, so a need saved
    under the theme's label counts as that theme's need.
    """
    themes = [t for t in (scope or {}).get("themes", ()) if isinstance(t, Mapping)]
    ids = {str(t.get("id")) for t in themes}
    folded: dict[str, str] = {}
    for t in themes:
        for name in (t.get("id"), t.get("name")):
            if name is not None:
                folded.setdefault(_folded(name), str(t.get("id")))

    def resolve(theme: Any) -> str | None:
        if theme is None:
            return None
        return str(theme) if str(theme) in ids else folded.get(_folded(theme))

    return resolve


def needs(
    content: dict[str, Any], reads: Mapping[str, Mapping[str, Any] | None] | None = None
) -> dict[str, Any]:
    """The sample's ``rows`` and ``unique``, each need's scope theme and "N of M" phrase.

    ``reads`` holds the current scope (``compute_reads``): a need's theme that names a scope
    theme by its name, or by its ID in other case, is written as that theme's ID.
    """
    listed = content.get("needs")
    resolve = theme_resolver((reads or {}).get("scope"))
    if isinstance(listed, list):
        for need in listed:
            if not isinstance(need, dict) or (theme := need_theme(need)) is None:
                continue
            need["theme"] = resolve(theme) or theme
    sample = content.get("sample")
    if not isinstance(sample, dict):
        return content
    duplicates = sample.get("duplicates", [])
    copies = len(duplicates) if isinstance(duplicates, list) else None
    unique, rows = _count(sample.get("unique")), _count(sample.get("rows"))
    ids = review_ids(sample)
    if copies is not None and ids and len(ids) >= copies:
        rows = sample["rows"] = len(ids)
        unique = sample["unique"] = rows - copies
    elif copies is not None and unique is not None:
        sample["rows"] = unique + copies
    elif copies is not None and rows is not None and rows >= copies:
        unique = sample["unique"] = rows - copies
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
