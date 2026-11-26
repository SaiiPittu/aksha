"""Web search and fetch values (ADR-P26 amendment A3): results, fetched pages, the record ID.

A search returns :class:`SearchResults`: ranked :class:`SearchHit` values, each a title, a
URL, a snippet and a date when the provider gives one. A hit is a pointer, never evidence:
the web tools mint no span over a snippet, so a claim needs the page fetched. A fetch
returns a :class:`FetchedPage`: the URL asked for, the URL that answered, the status, the
content type and the decoded text.

A fetched page becomes a record of the source :data:`WEB_SOURCE` (``web``) whose ID is
:func:`web_record_id` of the URL that answered (``web/site.example/page``), at the
version ``sha256:`` over its text, so a span over it is minted like any other record's
(ADR-P13 D1) and a citation names the page it came from.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from aksha.runtime.egress.types import EgressPolicy
from aksha.runtime.errors import AkshaError

WEB_SOURCE = "web"
"""The source every fetched page is a record of."""
WEB_COLLECTION = "web"
"""The collection of a fetched page's record ID: ``web/<host><path>``."""

WEB_EGRESS = "aksha.web"
"""The runtime's own egress policy of a web fetch: any public host over HTTPS, never a
private address (ADR-P26 A3). A tool names it as ``egress="aksha.web"``; no extension
declares it."""
WEB_POLICY = EgressPolicy(WEB_EGRESS, allow_hosts=("*",))
BUILTIN_EGRESS = {WEB_EGRESS: WEB_POLICY}
"""Egress policies the runtime declares itself, which any tool may name."""

SEARCH_FAILED = "search_failed"
FETCH_FAILED = "fetch_failed"
WEB_OFF = "web_off"
"""No search or fetch port is configured for this process (``AKSHA_SEARCH`` unset)."""
NOT_ON_THE_WEB = "not_found"
"""The URL answered 404, or a world's web has no page at it."""


class WebError(AkshaError):
    """A search or a fetch that did not answer: ``search_failed``, ``fetch_failed``, ..."""

    default_code = FETCH_FAILED


@dataclass(frozen=True)
class SearchHit:
    """One ranked result. ``rank`` counts from 1; ``date`` is ISO when the provider gives one."""

    rank: int
    title: str
    url: str
    snippet: str
    date: str | None = None

    def as_json(self) -> dict[str, Any]:
        """The hit as a tool result and a recording write it."""
        out: dict[str, Any] = {"rank": self.rank, "title": self.title, "url": self.url,
                               "snippet": self.snippet}  # fmt: skip
        if self.date:
            out["date"] = self.date
        return out

    @classmethod
    def from_json(cls, data: Any) -> SearchHit:
        """The hit :meth:`as_json` wrote."""
        return cls(int(data["rank"]), str(data["title"]), str(data["url"]),
                   str(data.get("snippet", "")), data.get("date") or None)  # fmt: skip


@dataclass(frozen=True)
class SearchResults:
    """What one search answered: the query, the provider that answered and its hits."""

    query: str
    provider: str
    hits: tuple[SearchHit, ...]

    def as_json(self) -> dict[str, Any]:
        """The results as a recording writes them."""
        return {"query": self.query, "provider": self.provider,
                "hits": [h.as_json() for h in self.hits]}  # fmt: skip

    @classmethod
    def from_json(cls, data: Any) -> SearchResults:
        """The results :meth:`as_json` wrote."""
        return cls(str(data["query"]), str(data.get("provider", "")),
                   tuple(SearchHit.from_json(h) for h in data.get("hits", ())))  # fmt: skip


@dataclass(frozen=True)
class FetchedPage:
    """One fetched page: the URL asked for, the URL that answered, status, type, text."""

    url: str
    final_url: str
    status: int
    content_type: str
    text: str
    truncated: bool = False

    def as_json(self) -> dict[str, Any]:
        """The page as a recording writes it."""
        return {"url": self.url, "final_url": self.final_url, "status": self.status,
                "content_type": self.content_type, "text": self.text,
                "truncated": self.truncated}  # fmt: skip

    @classmethod
    def from_json(cls, data: Any) -> FetchedPage:
        """The page :meth:`as_json` wrote."""
        return cls(str(data["url"]), str(data.get("final_url") or data["url"]),
                   int(data.get("status", 200)), str(data.get("content_type", "")),
                   str(data["text"]), bool(data.get("truncated", False)))  # fmt: skip


def normal_url(url: str) -> str:
    """``url`` with its scheme and host lower-cased and its fragment dropped."""
    parts = urlsplit(url.strip())
    query = f"?{parts.query}" if parts.query else ""
    netloc = (parts.hostname or "").lower()
    if parts.port:
        netloc += f":{parts.port}"
    return f"{parts.scheme.lower()}://{netloc}{parts.path or '/'}{query}"


def web_record_id(url: str) -> str:
    """The record ID of the page at ``url``: ``web/<host><path>``, its query kept.

    The root path is ``/index`` and a trailing slash is dropped, so ``https://a.example/``
    and ``https://a.example`` are one record, ``web/a.example/index``.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/") or "/index"
    query = f"?{parts.query}" if parts.query else ""
    return f"{WEB_COLLECTION}/{host}{path}{query}"


def text_version(text: str) -> str:
    """The version of a fetched text: ``sha256:`` over its UTF-8 bytes."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
