"""Record and replay the web: every search and fetch a run made, answered again offline.

:class:`RecordingSearch` and :class:`RecordingFetch` wrap the live ports and append each
answer to the recording (``search.jsonl``, ``fetch.jsonl``; :mod:`.sink` writes the
lines), a failed fetch with its error code. :class:`ReplayWeb` is both ports over a
recording: a search answers with the recorded hits for that exact query and limit, a
fetch with the recorded page (or the recorded error) for that URL, so a replay walks the
same pages without a network. A query or a URL the run never asked is ``replay_missing``: a replay
never invents a page.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from aksha.runtime.egress.errors import EgressRefused
from aksha.runtime.egress.ports import HttpClient
from aksha.runtime.web.ports import WebFetch, WebSearch
from aksha.runtime.web.types import FetchedPage, SearchResults, WebError, normal_url

from .sink import Sink

REPLAY_MISSING = "replay_missing"


def _search_key(query: str, limit: int) -> str:
    return f"{' '.join(query.split())}\x1f{limit}"


class RecordingSearch:
    """A search port that appends every answer to ``sink`` (module doc)."""

    def __init__(self, inner: WebSearch, sink: Sink) -> None:
        """Answer through ``inner``; record into ``sink``."""
        self.inner = inner
        self.sink = sink
        self.name = inner.name

    def search(self, query: str, *, limit: int) -> SearchResults:
        """The inner port's answer, recorded."""
        found = self.inner.search(query, limit=limit)
        self.sink.append("search", {"query": query, "limit": limit, "results": found.as_json()})
        return found


class RecordingFetch:
    """A fetch port that appends every page, or every refusal, to ``sink`` (module doc)."""

    def __init__(self, inner: WebFetch, sink: Sink) -> None:
        """Fetch through ``inner``; record into ``sink``."""
        self.inner = inner
        self.sink = sink
        self.name = inner.name

    def fetch(self, url: str, http: HttpClient) -> FetchedPage:
        """The inner port's page, recorded; a refusal is recorded and raised again."""
        try:
            page = self.inner.fetch(url, http)
        except (WebError, EgressRefused) as e:
            self.sink.append("fetch", {"url": url, "error": {"code": e.code, "message": e.message}})
            raise
        self.sink.append("fetch", {"url": url, "page": page.as_json()})
        return page


class ReplayWeb:
    """Both web ports over a recording's ``search`` and ``fetch`` lines (module doc)."""

    def __init__(
        self,
        searches: Iterable[Mapping[str, Any]],
        fetches: Iterable[Mapping[str, Any]],
        *,
        provider: str = "replay",
    ) -> None:
        """Answer from the recorded lines; the last answer for a query or URL wins."""
        self.name = provider
        self._searches = {
            _search_key(str(s["query"]), int(s["limit"])): SearchResults.from_json(s["results"])
            for s in searches
        }
        self._fetches = {normal_url(str(f["url"])): f for f in fetches}
        self.asked: list[str] = []

    def search(self, query: str, *, limit: int) -> SearchResults:
        """The recorded results of ``query`` at ``limit``; ``replay_missing`` otherwise."""
        self.asked.append(f"search:{query}")
        found = self._searches.get(_search_key(query, limit))
        if found is None:
            raise WebError(REPLAY_MISSING, f"the recording has no search for {query!r}")
        return found

    def fetch(self, url: str, http: HttpClient) -> FetchedPage:
        """The recorded page of ``url``, or its recorded refusal; ``replay_missing`` otherwise."""
        self.asked.append(f"fetch:{url}")
        found = self._fetches.get(normal_url(url))
        if found is None:
            raise WebError(REPLAY_MISSING, f"the recording has no fetch of {url}")
        if "error" in found:
            err = found["error"]
            raise WebError(str(err.get("code", "fetch_failed")), str(err.get("message", "")))
        return FetchedPage.from_json(found["page"])
