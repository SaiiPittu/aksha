"""The research agent's read tools: search, fetch a page, read reviews, read a note.

Every body reads through ``ctx.reader`` (the runtime's record reader for source
:data:`SOURCE`), never the file system, and returns the versions it read as ``Ok.reads``
so lineage reaches each record. ``fetch_page``, ``read_reviews`` and ``read_note`` mint
one citation span per passage, in passage order: the model is shown the span IDs after the
result and cites ``{"span": ID, "quote": ...}``. ``search`` mints nothing, so a snippet
can never be cited: a claim needs the page fetched.

A record outside a tool's collections, or one the reader cannot return, is an ``Err`` the
model reads (``not_in_collection``, ``fetch_failed``), and a claim it would have supported
stays ``unverified``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.api import (
    Err,
    InputRef,
    Ok,
    RetryPolicy,
    ReturnedText,
    SpanDraft,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
)

from . import parsing

AGENT = "research"
SOURCE = "corpus"
"""The versioned source the agent reads: a folder of pages, review exports and notes."""

PAGES = "competitors"
REVIEWS = "reviews"
NOTES = "priorities"
COLLECTIONS = (PAGES, REVIEWS, NOTES)

SNIPPET_CHARS = 160


def _record(args: Mapping[str, Any], allowed: tuple[str, ...]) -> str | Err:
    record = str(args["record"])
    parts = record.split("/")
    if len(parts) < 2 or parts[0] not in allowed or any(p in ("", ".", "..") for p in parts):
        return Err(
            "not_in_collection",
            f"{record!r} is not a record of {list(allowed)}",
            paths=("/record",),
            item=record,
        )
    return record


def _read(ctx: ToolContext, record: str) -> ReturnedText | Err:
    found = ctx.reader.read(SOURCE, record) if ctx.reader is not None else None
    if found is None:
        return Err("fetch_failed", f"{record} could not be read", item=record)
    return found


def _input(text: ReturnedText) -> InputRef:
    return InputRef("record", text.record_id, text.version)


def _drafts(text: ReturnedText, passages: tuple[parsing.Passage, ...]) -> tuple[SpanDraft, ...]:
    return tuple(SpanDraft(SOURCE, text.record_id, text.version, p.start, p.end) for p in passages)


def search(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """Records of one collection whose visible text holds any of ``terms`` (case folded)."""
    collection = str(args["collection"])
    if collection not in COLLECTIONS:
        return ToolOutcome(
            Err("not_in_collection", f"no collection {collection!r}", paths=("/collection",))
        )
    terms = [str(t).lower() for t in args["terms"]]
    records = ctx.reader.records(SOURCE, collection) if ctx.reader is not None else ()
    hits: list[dict[str, Any]] = []
    reads: list[InputRef] = []
    for record in records:
        found = ctx.reader.read(SOURCE, record) if ctx.reader is not None else None
        if found is None:
            continue
        reads.append(_input(found))
        text = parsing.visible(found.text)
        low = text.lower()
        at = min((low.find(t) for t in terms if t in low), default=-1)
        if at < 0:
            continue
        hit: dict[str, Any] = {"record": record, "snippet": text[max(0, at - 40) : at + 120]}
        if record.endswith(".html"):
            page = parsing.read_page(found.text)
            hit.update(title=page.title, published=page.published, url=page.url)
        hits.append(hit)
    data = {
        "collection": collection,
        "terms": list(args["terms"]),
        "records_read": len(reads),
        "checked": [f"record:{r.id}@{r.version}" for r in reads],
        "hits": hits,
        "note": "search snippets are not citable; fetch the page to cite it",
    }
    return ToolOutcome(Ok(data, reads=tuple(reads)))


def fetch_page(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """One product or pricing page as numbered passages, one citation span each."""
    record = _record(args, (PAGES,))
    if isinstance(record, Err):
        return ToolOutcome(record)
    found = _read(ctx, record)
    if isinstance(found, Err):
        return ToolOutcome(found)
    page = parsing.read_page(found.text)
    data = {
        "record": record,
        "version": found.version,
        "published": page.published,
        "title": page.title,
        "product": page.product,
        "brand": page.brand,
        "url": page.url,
        "passages": [p.as_json(n) for n, p in enumerate(page.passages)],
    }
    spans = _drafts(found, page.passages)
    return ToolOutcome(Ok(data, spans=spans, reads=(_input(found),)))


def read_reviews(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """A review export: every row with its fields, its text one citation span each."""
    record = _record(args, (REVIEWS,))
    if isinstance(record, Err):
        return ToolOutcome(record)
    found = _read(ctx, record)
    if isinstance(found, Err):
        return ToolOutcome(found)
    header, passages = parsing.read_reviews(found.text)
    rows = [{"n": n, **p.fields, "text": p.text} for n, p in enumerate(passages)]
    data = {"record": record, "version": found.version, **header, "rows": rows}
    return ToolOutcome(Ok(data, spans=_drafts(found, passages), reads=(_input(found),)))


def read_note(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """A text note as numbered lines, one citation span each."""
    record = _record(args, (NOTES,))
    if isinstance(record, Err):
        return ToolOutcome(record)
    found = _read(ctx, record)
    if isinstance(found, Err):
        return ToolOutcome(found)
    lines = parsing.read_lines(found.text)
    data = {
        "record": record,
        "version": found.version,
        "lines": [{"n": n, "text": p.text} for n, p in enumerate(lines)],
    }
    return ToolOutcome(Ok(data, spans=_drafts(found, lines), reads=(_input(found),)))


_RECORD = {
    "type": "object",
    "properties": {"record": {"type": "string", "minLength": 3}},
    "required": ["record"],
    "additionalProperties": False,
}
_SEARCH = {
    "type": "object",
    "properties": {
        "collection": {"enum": list(COLLECTIONS)},
        "terms": {"type": "array", "items": {"type": "string", "minLength": 2}, "minItems": 1},
    },
    "required": ["collection", "terms"],
    "additionalProperties": False,
}
_FETCH_RETRY = RetryPolicy(timeout_ms=10_000, max_retries=2)

TOOLS = (
    ToolDecl(
        ToolSpec(
            "search",
            AGENT,
            "read",
            "List records of a collection that mention any term. Snippets are not citable.",
            _SEARCH,
            collections=frozenset(COLLECTIONS),
        ),
        search,
    ),
    ToolDecl(
        ToolSpec(
            "fetch_page",
            AGENT,
            "read",
            "Fetch one product or pricing page as numbered, citable passages.",
            _RECORD,
            collections=frozenset({PAGES}),
            mints_citation_spans=True,
            retry=_FETCH_RETRY,
        ),
        fetch_page,
    ),
    ToolDecl(
        ToolSpec(
            "read_reviews",
            AGENT,
            "read",
            "Read a customer review export; each review's text is citable.",
            _RECORD,
            collections=frozenset({REVIEWS}),
            mints_citation_spans=True,
        ),
        read_reviews,
    ),
    ToolDecl(
        ToolSpec(
            "read_note",
            AGENT,
            "read",
            "Read a note (such as ranked priorities) as numbered, citable lines.",
            _RECORD,
            collections=frozenset({NOTES}),
            mints_citation_spans=True,
        ),
        read_note,
    ),
)
