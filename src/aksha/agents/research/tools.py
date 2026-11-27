"""The research agent's read tools: list the inputs, search them, read a page, a table, a text.

The agent reads one versioned source, :data:`SOURCE` (``inputs``), whose one collection
:data:`COLLECTION` (``input``) holds whatever the person uploaded or configured: saved web
pages, Markdown notes, CSV or JSON exports, emails, text extracted from a PDF, a slide deck
or a spreadsheet. No tool expects a folder layout, a file name or a column name: each
cuts a record by structure and asks the agent's aliases what it holds (``reading.py``),
and every tool reads every format, sniffed from the content.

Every body reads through ``ctx.reader`` (the runtime's record reader), never the file
system, and returns the versions it read as ``Ok.reads`` so lineage reaches each record.
``list_inputs`` and ``search`` are recorded queries (ADR-P05 D6): what they record is the
query and the set of records it found, so a file added later re-executes the query and
reopens only the work whose listing or search it changes. ``fetch_page``,
``read_table`` and ``read_note`` mint one citation span per passage, in passage order: the
model is shown the span IDs after the result and cites ``{"span": ID, "quote": ...}``.
``list_inputs`` and ``search`` mint nothing, so a listing or a snippet can never be
cited: a claim needs the record read. ``read_table`` pages through rows (``offset``,
``limit``, ``total``) and, given the scope's themes, says which themes each row speaks to,
so counting runs over the whole export, never over what fits in one result.

A record the reader cannot return is an ``Err`` the model reads (``fetch_failed``), and a
claim it would have supported stays ``unverified``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from aksha.runtime.api import (
    FORMATS,
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
    fetch_text,
    fetch_url_spec,
    visible,
    web_search_tool,
)

from . import parsing, reading

AGENT = "research"
SOURCE = "inputs"
"""The versioned source the agent reads: the person's uploaded or configured inputs."""
COLLECTION = "input"
COLLECTIONS = (COLLECTION,)

SNIPPET_CHARS = 160
PAGE_ROWS = 200
"""Rows ``read_table`` returns at most per call; ``total`` and ``next_offset`` say the rest."""
ALIASES = frozenset(
    {"doc_kind", "page_facts", "price_table", "report_facts", "conversation_facts",
     "feedback_items", "table_roles", "theme_match"}
)  # fmt: skip
"""The fast-class aliases the read tools ask (``reading.py``)."""


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
    return tuple(SpanDraft(text.source, text.record_id, text.version, p.start, p.end)
                 for p in passages)  # fmt: skip


def _query(ctx: ToolContext, tool: str, args: Mapping[str, Any], hits: list[str]) -> InputRef:
    """The recorded query of ``tool`` with ``args``: its version hashes the hits as a set."""
    qid = f"{ctx.agent}.{tool}?" + json.dumps(dict(args), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256("\n".join(sorted(set(hits))).encode("utf-8")).hexdigest()
    return InputRef("query", qid, "q-" + digest[:16])


def list_inputs(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """Input records with their format and kind, optionally only some kinds.

    The kind is the ``doc_kind`` alias's decision over a structural preview of the record
    (``reading.decide``); an abstention lists it as ``other`` with ``decided`` false. A
    recorded query: its version is the set of records listed with their kinds, so a page
    or an export added later re-executes it and reopens only the work that listed that
    kind. Nothing in a listing is citable.
    """
    kinds = [str(s) for s in args.get("kinds", ())]
    records = ctx.reader.records(SOURCE, COLLECTION) if ctx.reader is not None else ()
    rows = []
    for r in records:
        found = ctx.reader.read(SOURCE, r) if ctx.reader is not None else None
        if found is None:
            continue
        fmt = reading.record_format(r, found.text)
        kind, decided = reading.decide(ctx.models, found, fmt)
        parts = reading.decide_sections(ctx.models, found, fmt)
        if kinds and kind not in kinds and not any(p["kind"] in kinds for p in parts):
            continue
        row: dict[str, Any] = {"record": r, "format": fmt, "kind": kind, "decided": decided}
        if parts:
            row["sections"] = parts
        rows.append(row)
    keys = [f"{x['record']}:{x['kind']}:" + ",".join(p["kind"] for p in x.get("sections", ()))
            for x in rows]  # fmt: skip
    read = _query(ctx, "list_inputs", args, keys)
    data = {"inputs": rows, "count": len(rows), "query": f"query:{read.id}@{read.version}",
            "note": "read a record to cite it; an 'other' record is yours to read; a record "
                    "listed with sections holds several kinds: read the section that is the "
                    "kind you need (its 'section' number)"}  # fmt: skip
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
        found = ctx.reader.read(SOURCE, record) if ctx.reader is not None else None
        if found is None:
            continue
        fmt = reading.record_format(record, found.text)
        if formats and fmt not in formats:
            continue
        checked += 1
        text = visible(found.text) if fmt == "html" else " ".join(found.text.split())
        low = text.lower()
        at = min((low.find(t) for t in terms if t in low), default=-1)
        if at < 0:
            continue
        hit: dict[str, Any] = {"record": record, "format": fmt,
                               "snippet": text[max(0, at - 40) : at + 120]}  # fmt: skip
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


def _page(
    ctx: ToolContext, found: ReturnedText, section: int | None, extra: Mapping[str, Any]
) -> ToolOutcome:
    """A returned text read as a page: its identity and numbered passages, one span each."""
    record = found.record_id
    fmt = reading.record_format(record, found.text)
    segs = reading.section_of(found, fmt, section)
    if segs is None:
        return ToolOutcome(Err("no_section", f"{record} has no section {section}", item=record))
    page, read = reading.read_page(ctx.models, found, fmt, found.source, segs)
    data = {
        **extra,
        "record": record,
        "version": found.version,
        "format": fmt,
        **({} if section is None else {"section": section}),
        **read.as_json(),
        "published": page.published,
        "title": page.title,
        "product": page.product,
        "brand": page.brand,
        "url": extra.get("url", page.url),
        "passages": [p.as_json(n) for n, p in enumerate(page.passages)],
    }
    if read.needs_reader:
        data["note"] = (
            "the fast readers left " + ", ".join(read.unresolved) + " unresolved: read the "
            "passages yourself and cite them"
        )
    return ToolOutcome(Ok(data, spans=_drafts(found, page.passages), reads=(_input(found),)))


def fetch_page(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """Any input record as a page: its identity and numbered passages, one citation span each."""
    record = str(args["record"])
    found = _read(ctx, record)
    if isinstance(found, Err):
        return ToolOutcome(found)
    section = args.get("section")
    return _page(ctx, found, None if section is None else int(section), {})


def fetch_url(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """A web page by URL, read as :func:`fetch_page` reads an input (the same view).

    The page is fetched through the runtime's web handle, so its text is one the runtime
    returned and its passages are minted as spans of the source ``web``. ``url`` is the URL
    asked for; ``final_url`` the one that answered (after redirects).
    """
    fetched = fetch_text(ctx, str(args["url"]))
    if isinstance(fetched, Err):
        return ToolOutcome(fetched)
    extra = {"url": fetched.page.url, "final_url": fetched.page.final_url}
    return _page(ctx, fetched.text, None, extra)


def read_table(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """Rows of a table (any delimiter, JSON at any depth) or the voices of a text, by page."""
    record = str(args["record"])
    found = _read(ctx, record)
    if isinstance(found, Err):
        return ToolOutcome(found)
    fmt = reading.record_format(record, found.text)
    offset = int(args.get("offset", 0))
    limit = int(args.get("limit", PAGE_ROWS))
    themes = [dict(t) for t in args.get("themes", ())]
    section = None if args.get("section") is None else int(args["section"])
    try:
        table = reading.read_table(ctx.models, found, fmt, SOURCE, offset=offset, limit=limit,
                                   themes=themes, section=section)  # fmt: skip
    except ValueError as e:
        return ToolOutcome(Err("unreadable", f"{record}: {e}", item=record))
    rows = [{"n": n, **p.fields, "text": p.text} for n, p in enumerate(table.passages)]
    end = offset + limit
    data = {
        "record": record,
        "version": found.version,
        "format": fmt,
        **({} if section is None else {"section": section}),
        **table.header,
        "columns": list(table.columns),
        "roles": dict(table.roles),
        "offset": offset,
        "total": table.total,
        "next_offset": end if end < table.total else None,
        "rows": rows,
    }
    return ToolOutcome(Ok(data, spans=_drafts(found, table.passages), reads=(_input(found),)))


def read_note(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """Any text record (a note, an email, extracted PDF text) as numbered, citable blocks."""
    record = str(args["record"])
    found = _read(ctx, record)
    if isinstance(found, Err):
        return ToolOutcome(found)
    fmt = reading.record_format(record, found.text)
    lines = reading.read_lines(found, fmt)
    data = {
        "record": record,
        "version": found.version,
        "format": fmt,
        "lines": [{"n": n, "text": p.text} for n, p in enumerate(lines)],
    }
    return ToolOutcome(Ok(data, spans=_drafts(found, lines), reads=(_input(found),)))


_SECTION = {"type": "integer", "minimum": 0}
"""A section number from ``list_inputs``: read only that section of the record."""
_RECORD = {
    "type": "object",
    "properties": {"record": {"type": "string", "minLength": 3}},
    "required": ["record"],
    "additionalProperties": False,
}
_PAGE = {
    "type": "object",
    "properties": {"record": {"type": "string", "minLength": 3}, "section": _SECTION},
    "required": ["record"],
    "additionalProperties": False,
}
_LIST = {
    "type": "object",
    "properties": {"kinds": {"type": "array", "items": {"enum": list(reading.KIND_NAMES)}}},
    "additionalProperties": False,
}
_SEARCH = {
    "type": "object",
    "properties": {
        "terms": {"type": "array", "items": {"type": "string", "minLength": 2}, "minItems": 1},
        "formats": {"type": "array", "items": {"enum": list(FORMATS)}},
    },
    "required": ["terms"],
    "additionalProperties": False,
}
_TABLE = {
    "type": "object",
    "properties": {
        "record": {"type": "string", "minLength": 3},
        "section": _SECTION,
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "name": {"type": "string"},
                    "terms": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id"],
            },
        },
    },
    "required": ["record"],
    "additionalProperties": False,
}
_FETCH_RETRY = RetryPolicy(timeout_ms=120_000, max_retries=2)
"""A read asks fast-class models: a live one may take seconds per record."""
_LIST_RETRY = RetryPolicy(timeout_ms=600_000, max_retries=1)
_INPUTS = frozenset(COLLECTIONS)

TOOLS = (
    web_search_tool(AGENT),
    ToolDecl(
        fetch_url_spec(
            AGENT,
            "Fetch a web page by URL and read it as fetch_page reads an input: its kind, then "
            "numbered, citable passages (who it is from, when, each stated price, claims, "
            "relationships, hidden text). Cite a passage, never a search snippet.",
            retry=_FETCH_RETRY,
            model_aliases=ALIASES,
        ),
        fetch_url,
    ),
    ToolDecl(
        ToolSpec(
            "list_inputs",
            AGENT,
            "read",
            "List the input records with their format (html, markdown, csv, json, email, "
            "text) and kind (" + ", ".join(reading.KIND_NAMES) + "), optionally only some "
            "kinds. Nothing in the listing is citable.",
            _LIST,
            collections=_INPUTS,
            recorded_query=True,
            result_key="/record",
            retry=_LIST_RETRY,
            model_aliases=ALIASES,
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
            "Read any input: its kind, then numbered, citable passages: who it is from, "
            "when, each stated price (with its seller in a price table), specifications, "
            "claims, relationships and hidden text. 'unresolved' names what you must read "
            "from the passages yourself. 'section' reads one section the listing names.",
            _PAGE,
            collections=_INPUTS,
            mints_citation_spans=True,
            retry=_FETCH_RETRY,
            model_aliases=ALIASES,
        ),
        fetch_page,
    ),
    ToolDecl(
        ToolSpec(
            "read_table",
            AGENT,
            "read",
            "Read customer voices: the rows of a table (any delimiter, JSON rows at any "
            "depth) or each voice in a text, by page (offset, limit; total says how many). "
            "Each row's text is citable, its other columns named by what they hold; given "
            "themes, each row says which themes it speaks to. 'section' reads one section "
            "of a text the listing names.",
            _TABLE,
            collections=_INPUTS,
            mints_citation_spans=True,
            retry=_LIST_RETRY,
            model_aliases=ALIASES,
        ),
        read_table,
    ),
    ToolDecl(
        ToolSpec(
            "read_note",
            AGENT,
            "read",
            "Read any input (a note, an email, a brochure, a deck) as numbered, citable blocks.",
            _RECORD,
            collections=_INPUTS,
            mints_citation_spans=True,
        ),
        read_note,
    ),
)
