"""The generic web tools every agent may list: ``web_search`` and the parts of ``fetch_url``.

``web_search(query, limit)`` returns ranked results (title, url, snippet, date) and mints
nothing: a snippet is a pointer, never evidence. :func:`web_search_tool` builds it for
an agent. ``fetch_url(url, offset?, limit?, cells?, extract?)`` fetches the page and
shows it as the shared ``read_document`` shows an input (``inputs/tools.read_view``):
numbered lines and citable units; :func:`fetch_url_tool` builds it, with the agent's
``InputReading`` when ``extract`` should decide the page's kind. An agent that reads pages
its own way may still write its own body over :func:`fetch_text` and keep
:func:`fetch_url_spec`. A fetch is a read with the
egress policy :data:`WEB_EGRESS`: on a live web, every hop is checked (ADR-P26). Both
tools are declared under :data:`WEB_EGRESS`, and only a tool under it is handed
``ToolContext.web`` (``loop.tier.web_for``), so no other tool can search or fetch.

A fetch's text reaches lineage as a record read (``InputRef("record", "web/...", version)``)
and its passages are minted as spans of the source ``web`` (ADR-P13 D1).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.grounding.segment import FORMATS
from aksha.runtime.inputs.tools import InputReading, read_schema, read_view
from aksha.runtime.tools.types import (
    Err,
    Ok,
    RetryPolicy,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
)
from aksha.runtime.web.attempt import FetchedText
from aksha.runtime.web.types import BUILTIN_EGRESS, WEB_EGRESS, WEB_OFF, WebError, url_problem

SNIPPET_NOTE = "a snippet is not evidence: fetch_url a result to read and cite it"
_RETRY = RetryPolicy(timeout_ms=60_000, max_retries=2)

SEARCH_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 2, "maxLength": 400},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
    },
    "required": ["query"],
    "additionalProperties": False,
}
FETCH_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {"url": {"type": "string", "minLength": 8, "maxLength": 2000}},
    "required": ["url"],
    "additionalProperties": False,
}


def web_search(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    """Ranked results for ``query``; nothing in them is citable."""
    if ctx.web is None:
        return ToolOutcome(Err(WEB_OFF, "no web search is configured"))
    query = " ".join(str(args["query"]).split())
    try:
        found = ctx.web.search(query, limit=int(args.get("limit", 10)))
    except WebError as e:
        return ToolOutcome(Err(e.code, e.message))
    data = {"query": query, "provider": found.provider,
            "results": [h.as_json() for h in found.hits], "note": SNIPPET_NOTE}  # fmt: skip
    return ToolOutcome(Ok(data))


def web_search_tool(agent: str) -> ToolDecl:
    """``web_search`` for ``agent`` (module doc)."""
    spec = ToolSpec(
        "web_search",
        agent,
        "read",
        "Search the web. Returns ranked results (title, url, snippet, date). Results mix "
        "relevant pages with noise: copies, outdated versions, adverts and pages that only "
        "share a word. A snippet is never evidence: fetch_url the results you need.",
        SEARCH_SCHEMA,
        egress=WEB_EGRESS,
        retry=_RETRY,
    )
    return ToolDecl(spec, web_search)


def fetch_url_spec(agent: str, description: str = "", **extra: Any) -> ToolSpec:
    """The ``fetch_url`` spec for ``agent``: a read under :data:`WEB_EGRESS` that mints spans."""
    text = description or (
        "Fetch one web page by URL and read it as numbered, citable passages. Cite a "
        "passage, never a search snippet."
    )
    return ToolSpec("fetch_url", agent, "read", text, FETCH_SCHEMA, mints_citation_spans=True,
                    egress=WEB_EGRESS, retry=extra.pop("retry", _RETRY), **extra)  # fmt: skip


def fetch_text(ctx: ToolContext, url: str) -> FetchedText | Err:
    """The page at ``url`` as a returned text, or the ``Err`` the model reads."""
    if ctx.web is None:
        return Err(WEB_OFF, "no web fetch is configured")
    if not url.lower().startswith(("https://", "http://")):
        return Err("url_invalid", f"{url!r} is not an http(s) URL", paths=("/url",))
    problem = url_problem(url)
    if problem is not None:
        return Err("url_invalid", f"{url!r}: {problem}", paths=("/url",))
    try:
        return ctx.web.fetch(url)
    except WebError as e:
        return Err(e.code, e.message, item=url)


def page_read(fetched: FetchedText) -> InputRef:
    """The lineage read of a fetched page: its record at its version."""
    return InputRef("record", fetched.text.record_id, fetched.text.version)


def fetch_url_schema(reading: InputReading | None = None) -> dict[str, Any]:
    """The generic ``fetch_url``'s arguments: the URL and ``read_document``'s window."""
    props = dict(read_schema(reading or InputReading("web", "web")))
    props = {k: v for k, v in props["properties"].items() if k not in ("ref", "section", "where")}
    return {"type": "object", "properties": {**FETCH_SCHEMA["properties"], **props},
            "required": ["url"], "additionalProperties": False}  # fmt: skip


def fetch_url(
    ctx: ToolContext, args: Mapping[str, Any], reading: InputReading | None = None
) -> ToolOutcome:
    """The generic body: the page as ``read_document`` shows an input (``read_view``)."""
    fetched = fetch_text(ctx, str(args["url"]))
    if isinstance(fetched, Err):
        return ToolOutcome(fetched)
    extra = {"url": fetched.page.url, "final_url": fetched.page.final_url,
             "content_type": fetched.page.content_type}  # fmt: skip
    return read_view(ctx, fetched.text, args, reading or InputReading("web", "web"), extra)


def fetch_url_tool(agent: str, reading: InputReading | None = None) -> ToolDecl:
    """The generic ``fetch_url`` for ``agent``: the page read as an input is read.

    With ``reading`` (the agent's :class:`InputReading`), ``extract`` decides the page's
    kind and fills its typed fields, as ``read_document`` does for an input.
    """
    aliases = frozenset() if reading is None else reading.aliases()
    spec = ToolSpec(
        "fetch_url",
        agent,
        "read",
        "Fetch one web page by URL and read it as read_document reads an input: numbered "
        "lines (offset, limit) and its units, the citable passages, each with its anchor. "
        "Cite a unit, never a search snippet."
        + (" extract=true also decides the page's kind and fills its typed fields."
           if aliases else ""),
        fetch_url_schema(reading),
        mints_citation_spans=True,
        egress=WEB_EGRESS,
        retry=_RETRY if reading is None else reading.retry,
        model_aliases=aliases,
    )

    def body(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        return fetch_url(ctx, args, reading)

    body.__name__ = "fetch_url"
    return ToolDecl(spec, body)


__all__ = ["BUILTIN_EGRESS", "FORMATS", "WEB_EGRESS", "fetch_text", "fetch_url_spec",
           "fetch_url_tool", "page_read", "web_search_tool"]  # fmt: skip
