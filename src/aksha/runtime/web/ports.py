"""The web ports (ADR-P26 amendment A3): searching the web, and fetching one page.

Both are adapters the composition root picks (``AKSHA_SEARCH``): a world's offline web
(``adapters.search.world``), a live search provider with plain HTTP fetch
(``adapters.search.brave``, ``adapters.web.http``), or a recording's answers
(``adapters.web.recording``). A tool body never holds one: it calls ``ToolContext.web``,
which the agent loop builds per attempt (:class:`aksha.runtime.web.attempt.AttemptWeb`)
so that a fetched page is a text the runtime returned and may be cited.

A live fetch reaches the network only through the ``HttpClient`` it is handed, the tool's
egress-policed client (ADR-P26 D2), so every hop is checked and recorded. An offline
adapter ignores it.
"""

from __future__ import annotations

from typing import Protocol

from aksha.runtime.egress.ports import HttpClient
from aksha.runtime.web.types import FetchedPage, SearchResults


class WebSearch(Protocol):
    """A search provider: a query to ranked results."""

    name: str

    def search(self, query: str, *, limit: int) -> SearchResults:
        """At most ``limit`` ranked results for ``query``; raises ``WebError`` when it cannot."""
        ...


class WebFetch(Protocol):
    """Fetches one page by URL."""

    name: str

    def fetch(self, url: str, http: HttpClient) -> FetchedPage:
        """The page at ``url``; raises ``WebError`` (or ``EgressRefused``) when it cannot."""
        ...
