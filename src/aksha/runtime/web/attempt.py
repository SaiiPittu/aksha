"""The web handle one attempt of a tool body sees (``ToolContext.web``).

:class:`AttemptWeb` is built by the agent loop for each attempt, over the process's search
and fetch ports, the attempt's reader and the attempt's HTTP client (the tool's
egress-policed client). :meth:`AttemptWeb.fetch` hands the fetched text to the attempt's
reader as a text the runtime returned (``AttemptReader.keep``), so a body may draft spans
over a page it fetched and over nothing else, exactly as over a record it read (ADR-P13
D1). :meth:`AttemptWeb.search` returns hits only: nothing in them is kept, so a snippet
can never be minted, and so never cited.

Without a port the handle refuses with ``web_off``: the default process has no web.
"""

from __future__ import annotations

from dataclasses import dataclass

from aksha.runtime.egress.ports import HttpClient
from aksha.runtime.grounding.reading import AttemptReader
from aksha.runtime.grounding.types import ReturnedText
from aksha.runtime.web.ports import WebFetch, WebSearch
from aksha.runtime.web.types import (
    FETCH_FAILED,
    NOT_ON_THE_WEB,
    WEB_OFF,
    WEB_SOURCE,
    FetchedPage,
    SearchResults,
    WebError,
    text_version,
    web_record_id,
)

MAX_RESULTS = 20
"""The most hits one search returns, whatever a body asks."""


@dataclass(frozen=True)
class FetchedText:
    """A fetched page as a body sees it: the page, and its text as a returned record."""

    page: FetchedPage
    text: ReturnedText

    @property
    def record_id(self) -> str:
        """The page's record ID (``web/<host><path>``)."""
        return self.text.record_id


class AttemptWeb:
    """One attempt's web: search and fetch through the ports, fetched texts kept (module doc)."""

    def __init__(
        self,
        search: WebSearch | None,
        fetch: WebFetch | None,
        reader: AttemptReader,
        http: HttpClient,
    ) -> None:
        """Search through ``search``, fetch through ``fetch`` over ``http``, keep in ``reader``."""
        self._search = search
        self._fetch = fetch
        self._reader = reader
        self._http = http

    @property
    def provider(self) -> str | None:
        """The search port's name, or None when there is none."""
        return None if self._search is None else self._search.name

    def search(self, query: str, *, limit: int = 10) -> SearchResults:
        """Ranked hits for ``query``; raises ``WebError("web_off")`` with no search port."""
        if self._search is None:
            raise WebError(WEB_OFF, "no web search is configured (AKSHA_SEARCH)")
        return self._search.search(query, limit=max(1, min(int(limit), MAX_RESULTS)))

    def fetch(self, url: str) -> FetchedText:
        """The page at ``url``, its text kept as returned; raises ``WebError`` when it fails."""
        if self._fetch is None:
            raise WebError(WEB_OFF, "no web fetch is configured (AKSHA_SEARCH)")
        page = self._fetch.fetch(url, self._http)
        if page.status == 404:
            raise WebError(NOT_ON_THE_WEB, f"{url} answered 404")
        if page.status >= 400:
            raise WebError(FETCH_FAILED, f"{url} answered {page.status}")
        text = ReturnedText(WEB_SOURCE, web_record_id(page.final_url or url),
                            text_version(page.text), page.text)  # fmt: skip
        self._reader.keep(text)
        return FetchedText(page, text)
