"""Record and replay the web: every search and fetch a run made, answered again offline.

:class:`RecordingSearch` and :class:`RecordingFetch` wrap the live ports and append each
answer to the recording (``search.jsonl``, ``fetch.jsonl``; :mod:`.sink` writes the
lines), a failure with its error code and whether it was transient. :class:`ReplayWeb` is
both ports over a recording: a search answers with the recorded hits for that exact query
and limit, a fetch with the recorded page (or the recorded error) for that URL, in the
order the run asked them (the last answer repeats), so a page that changed between two
fetches of one run, or a search that failed once and was retried, replays as it happened.
A query or a URL the run never asked is ``replay_missing``: a replay never invents a page.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from aksha.runtime.egress.errors import EgressRefused
from aksha.runtime.egress.ports import HttpClient
from aksha.runtime.execution.errors import TransientStepError
from aksha.runtime.web.ports import WebFetch, WebSearch
from aksha.runtime.web.types import FetchedPage, SearchResults, WebError, normal_url

from .sink import Sink

REPLAY_MISSING = "replay_missing"


def _search_key(query: str, limit: int) -> str:
    return f"{' '.join(query.split())}\x1f{limit}"


def _error(e: WebError | EgressRefused | TransientStepError) -> dict[str, Any]:
    out: dict[str, Any] = {"code": e.code, "message": e.message,
                           "transient": isinstance(e, TransientStepError)}  # fmt: skip
    if isinstance(e, EgressRefused):
        out["egress"] = {"host": e.host, "reason": e.reason, "url": e.url}
    return out


def _raise(err: Mapping[str, Any], default: str) -> None:
    code, message = str(err.get("code", default)), str(err.get("message", ""))
    refused = err.get("egress")
    if isinstance(refused, Mapping):
        raise EgressRefused(str(refused.get("host", "")), str(refused.get("reason", "")),
                            str(refused.get("url", "")))  # fmt: skip
    if err.get("transient"):
        raise TransientStepError(code, message)
    raise WebError(code, message)


class _InOrder:
    """Recorded answers by key, served in recorded order; the last one repeats."""

    def __init__(self) -> None:
        self.lines: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        self._next: dict[str, int] = defaultdict(int)

    def add(self, key: str, line: Mapping[str, Any]) -> None:
        self.lines[key].append(line)

    def take(self, key: str) -> Mapping[str, Any] | None:
        found = self.lines.get(key)
        if not found:
            return None
        n = self._next[key]
        self._next[key] = n + 1
        return found[min(n, len(found) - 1)]


class RecordingSearch:
    """A search port that appends every answer to ``sink`` (module doc)."""

    def __init__(self, inner: WebSearch, sink: Sink) -> None:
        """Answer through ``inner``; record into ``sink``."""
        self.inner = inner
        self.sink = sink
        self.name = inner.name

    def search(self, query: str, *, limit: int) -> SearchResults:
        """The inner port's answer, recorded; a failure is recorded and raised again."""
        try:
            found = self.inner.search(query, limit=limit)
        except (WebError, TransientStepError) as e:
            self.sink.append("search", {"query": query, "limit": limit, "error": _error(e)})
            raise
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
        except (WebError, EgressRefused, TransientStepError) as e:
            self.sink.append("fetch", {"url": url, "error": _error(e)})
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
        """Answer from the recorded lines, in their order (module doc)."""
        self.name = provider
        self._searches = _InOrder()
        for s in searches:
            self._searches.add(_search_key(str(s["query"]), int(s["limit"])), s)
        self._fetches = _InOrder()
        for f in fetches:
            self._fetches.add(normal_url(str(f["url"])), f)
        self.asked: list[str] = []

    def search(self, query: str, *, limit: int) -> SearchResults:
        """The recorded results of ``query`` at ``limit``; ``replay_missing`` otherwise."""
        self.asked.append(f"search:{query}")
        found = self._searches.take(_search_key(query, limit))
        if found is None:
            raise WebError(REPLAY_MISSING, f"the recording has no search for {query!r}")
        if "error" in found:
            _raise(found["error"], "search_failed")
        return SearchResults.from_json(found["results"])

    def fetch(self, url: str, http: HttpClient) -> FetchedPage:
        """The recorded page of ``url``, or its recorded refusal; ``replay_missing`` otherwise."""
        self.asked.append(f"fetch:{url}")
        found = self._fetches.take(normal_url(url))
        if found is None:
            raise WebError(REPLAY_MISSING, f"the recording has no fetch of {url}")
        if "error" in found:
            _raise(found["error"], "fetch_failed")
        return FetchedPage.from_json(found["page"])
