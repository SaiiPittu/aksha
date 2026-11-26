"""The research agent's read tools: list the inputs, search them, read a page, a table, a text.

The agent reads one versioned source, :data:`SOURCE` (``inputs``), whose one collection
:data:`COLLECTION` (``input``) holds whatever the person uploaded or configured: saved web
pages, Markdown notes, CSV or JSON exports, emails, text extracted from a PDF. No tool
expects a folder layout or a file name; each reads by format (``parsing.input_format``).

Every body reads through ``ctx.reader`` (the runtime's record reader), never the file
system, and returns the versions it read as ``Ok.reads`` so lineage reaches each record.
``list_inputs`` and ``search`` are recorded queries (ADR-P05 D6): what they record is the
query and the set of records it found, so a file added later re-executes the query and
reopens only the work whose listing or search it changes. ``fetch_page``,
``read_table`` and ``read_note`` mint one citation span per passage, in passage order: the
model is shown the span IDs after the result and cites ``{"span": ID, "quote": ...}``.
``list_inputs`` and ``search`` mint nothing, so a listing or a snippet can never be
cited: a claim needs the record read.

A record the reader cannot return is an ``Err`` the model reads (``fetch_failed``), and a
claim it would have supported stays ``unverified``; a record of the wrong format for a
tool is ``wrong_format``.
"""

from __future__ import annotations

import hashlib
import json
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
SOURCE = "inputs"
"""The versioned source the agent reads: the person's uploaded or configured inputs."""
COLLECTION = "input"
COLLECTIONS = (COLLECTION,)

PAGE_FORMATS = ("html", "markdown")
TABLE_FORMATS = ("csv", "json")
SNIPPET_CHARS = 160


def _not_an_input(record: str) -> Err | None:
    """A record ID that cannot name an input (empty, the collection itself, a ``..``)."""
    if not record or record == COLLECTION or ".." in record.split("/"):
        return Err("not_an_input", f"{record!r} is not an input record", paths=("/record",))
    return None


def _read(ctx: ToolContext, record: str) -> ReturnedText | Err:
    bad = _not_an_input(record)
    if bad is not None:
        return bad
    found = ctx.reader.read(SOURCE, record) if ctx.reader is not None else None
    if found is None:
        return Err("fetch_failed", f"{record} could not be read", item=record)
    return found


def _input(text: ReturnedText) -> InputRef:
    return InputRef("record", text.record_id, text.version)


def _drafts(text: ReturnedText, passages: tuple[parsing.Passage, ...]) -> tuple[SpanDraft, ...]:
    return tuple(SpanDraft(SOURCE, text.record_id, text.version, p.start, p.end) for p in passages)


def _wrong(record: str, fmt: str, want: tuple[str, ...]) -> ToolOutcome:
    bad = _not_an_input(record)
    if bad is not None:
        return ToolOutcome(bad)
    return ToolOutcome(
        Err("wrong_format", f"{record} is {fmt}; this tool reads {list(want)}", item=record)
    )


def _query(ctx: ToolContext, tool: str, args: Mapping[str, Any], hits: list[str]) -> InputRef:
    """The recorded query of ``tool`` with ``args``: its version hashes the hits as a set."""
    qid = f"{ctx.agent}.{tool}?" + json.dumps(dict(args), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256("\n".join(sorted(set(hits))).encode("utf-8")).hexdigest()
    return InputRef("query", qid, "q-" + digest[:16])


def list_inputs(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """Input records with their format and shape, optionally only some shapes.

    A recorded query: its version is the set of records listed, so a page or an export
    added later re-executes it and reopens only the work that listed that shape.
    Nothing in a listing is citable.
    """
    shapes = [str(s) for s in args.get("shapes", ())]
    records = ctx.reader.records(SOURCE, COLLECTION) if ctx.reader is not None else ()
    rows = []
    for r in records:
        found = ctx.reader.read(SOURCE, r) if ctx.reader is not None else None
        if found is None:
            continue
        shape = parsing.shape_of(r, found.text)
        if shapes and shape not in shapes:
            continue
        rows.append({"record": r, "format": parsing.input_format(r), "shape": shape})
    read = _query(ctx, "list_inputs", args, [f"{x['record']}:{x['shape']}" for x in rows])
    data = {"inputs": rows, "count": len(rows), "query": f"query:{read.id}@{read.version}",
            "note": "read a record to cite it"}  # fmt: skip
    return ToolOutcome(Ok(data, reads=(read,)))


def search(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """Records whose visible text holds any of ``terms`` (case folded), optionally by format.

    A recorded query, like :func:`list_inputs`: its version is the set of hits.
    """
    terms = [str(t).lower() for t in args["terms"]]
    formats = [str(f) for f in args.get("formats", ())]
    records = ctx.reader.records(SOURCE, COLLECTION) if ctx.reader is not None else ()
    hits: list[dict[str, Any]] = []
    checked = 0
    for record in records:
        fmt = parsing.input_format(record)
        if formats and fmt not in formats:
            continue
        found = ctx.reader.read(SOURCE, record) if ctx.reader is not None else None
        if found is None:
            continue
        checked += 1
        text = parsing.visible(found.text) if fmt == "html" else " ".join(found.text.split())
        low = text.lower()
        at = min((low.find(t) for t in terms if t in low), default=-1)
        if at < 0:
            continue
        hit: dict[str, Any] = {"record": record, "format": fmt,
                               "snippet": text[max(0, at - 40) : at + 120]}  # fmt: skip
        if fmt == "html":
            page = parsing.read_page(found.text)
            hit.update(title=page.title, published=page.published, url=page.url)
        hits.append(hit)
    read = _query(ctx, "search", args, [str(h["record"]) for h in hits])
    data = {
        "terms": list(args["terms"]),
        "records_checked": checked,
        "query": f"query:{read.id}@{read.version}",
        "hits": hits,
        "note": "search snippets are not citable; read the record to cite it",
    }
    return ToolOutcome(Ok(data, reads=(read,)))


def fetch_page(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """One page (HTML or Markdown) as numbered passages, one citation span each."""
    record = str(args["record"])
    fmt = parsing.input_format(record)
    if fmt not in PAGE_FORMATS:
        return _wrong(record, fmt, PAGE_FORMATS)
    found = _read(ctx, record)
    if isinstance(found, Err):
        return ToolOutcome(found)
    page = (
        parsing.read_page(found.text) if fmt == "html" else parsing.read_markdown_page(found.text)
    )
    data = {
        "record": record,
        "version": found.version,
        "format": fmt,
        "published": page.published,
        "title": page.title,
        "product": page.product,
        "brand": page.brand,
        "url": page.url,
        "passages": [p.as_json(n) for n, p in enumerate(page.passages)],
    }
    spans = _drafts(found, page.passages)
    return ToolOutcome(Ok(data, spans=spans, reads=(_input(found),)))


def read_table(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """A table (CSV, or JSON rows): every row with its fields, its text one citation span."""
    record = str(args["record"])
    fmt = parsing.input_format(record)
    if fmt not in TABLE_FORMATS:
        return _wrong(record, fmt, TABLE_FORMATS)
    found = _read(ctx, record)
    if isinstance(found, Err):
        return ToolOutcome(found)
    try:
        table = parsing.read_table(found.text, fmt)
    except ValueError as e:
        return ToolOutcome(Err("unreadable", f"{record}: {e}", item=record))
    rows = [{"n": n, **p.fields, "text": p.text} for n, p in enumerate(table.passages)]
    data = {
        "record": record,
        "version": found.version,
        "format": fmt,
        **table.header,
        "columns": list(table.columns),
        "roles": dict(table.roles),
        "rows": rows,
    }
    return ToolOutcome(Ok(data, spans=_drafts(found, table.passages), reads=(_input(found),)))


def read_note(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """Any text record (a note, an email, extracted PDF text) as numbered, citable lines."""
    record = str(args["record"])
    found = _read(ctx, record)
    if isinstance(found, Err):
        return ToolOutcome(found)
    lines = parsing.read_lines(found.text)
    data = {
        "record": record,
        "version": found.version,
        "format": parsing.input_format(record),
        "lines": [{"n": n, "text": p.text} for n, p in enumerate(lines)],
    }
    return ToolOutcome(Ok(data, spans=_drafts(found, lines), reads=(_input(found),)))


_RECORD = {
    "type": "object",
    "properties": {"record": {"type": "string", "minLength": 3}},
    "required": ["record"],
    "additionalProperties": False,
}
_LIST = {
    "type": "object",
    "properties": {"shapes": {"type": "array", "items": {"enum": list(parsing.SHAPES)}}},
    "additionalProperties": False,
}
_SEARCH = {
    "type": "object",
    "properties": {
        "terms": {"type": "array", "items": {"type": "string", "minLength": 2}, "minItems": 1},
        "formats": {"type": "array", "items": {"enum": list(parsing.FORMATS)}},
    },
    "required": ["terms"],
    "additionalProperties": False,
}
_FETCH_RETRY = RetryPolicy(timeout_ms=10_000, max_retries=2)
_INPUTS = frozenset(COLLECTIONS)

TOOLS = (
    ToolDecl(
        ToolSpec(
            "list_inputs",
            AGENT,
            "read",
            "List the input records with their format (html, markdown, csv, json, email, "
            "text) and shape (seller_page, review_table, table, note), optionally only some "
            "shapes. Nothing in the listing is citable.",
            _LIST,
            collections=_INPUTS,
            recorded_query=True,
            result_key="/record",
        ),
        list_inputs,
    ),
    ToolDecl(
        ToolSpec(
            "search",
            AGENT,
            "read",
            "List input records that mention any term, optionally of some formats. "
            "Snippets are not citable.",
            _SEARCH,
            collections=_INPUTS,
            recorded_query=True,
            result_key="/record",
        ),
        search,
    ),
    ToolDecl(
        ToolSpec(
            "fetch_page",
            AGENT,
            "read",
            "Read one page (HTML or Markdown) as numbered, citable passages: names, dates, "
            "prices, specifications, claims and hidden text.",
            _RECORD,
            collections=_INPUTS,
            mints_citation_spans=True,
            retry=_FETCH_RETRY,
        ),
        fetch_page,
    ),
    ToolDecl(
        ToolSpec(
            "read_table",
            AGENT,
            "read",
            "Read a table (CSV, or the rows of a JSON file) such as a review export; each "
            "row's text is citable, its other columns named by what they hold.",
            _RECORD,
            collections=_INPUTS,
            mints_citation_spans=True,
        ),
        read_table,
    ),
    ToolDecl(
        ToolSpec(
            "read_note",
            AGENT,
            "read",
            "Read any text input (a note, an email, text extracted from a PDF) as numbered, "
            "citable lines.",
            _RECORD,
            collections=_INPUTS,
            mints_citation_spans=True,
        ),
        read_note,
    ),
)
