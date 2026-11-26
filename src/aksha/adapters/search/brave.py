"""A live web search adapter over the Brave Search API (``AKSHA_SEARCH=brave``).

One GET per query to ``<base>/res/v1/web/search?q=<query>&count=<n>`` with the key in the
``X-Subscription-Token`` header; each ``web.results`` item becomes a hit (its ``title``,
``url``, ``description`` as the snippet with tags removed, and ``page_age`` as the date
when it gives one). The key is read from the environment (``AKSHA_BRAVE_API_KEY``) by the
composition root and never recorded; ``AKSHA_BRAVE_BASE_URL`` points the adapter at
another server (tests run it against a stub on localhost).

The adapter calls one fixed provider host, so it does not go through a tool's egress
policy; the pages a hit names are fetched through ``fetch_url``, which does. A 429 or a 5xx
answer, or a timeout, is a ``TransientStepError`` the tool's retry policy retries; any
other failure is a ``WebError("search_failed")`` the model reads.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

from aksha.runtime.execution.errors import TransientStepError
from aksha.runtime.web.types import SEARCH_FAILED, SearchHit, SearchResults, WebError

DEFAULT_BASE_URL = "https://api.search.brave.com"
KEY_ENV = "AKSHA_BRAVE_API_KEY"
BASE_ENV = "AKSHA_BRAVE_BASE_URL"
TIMEOUT_S = 20.0
_TAG = re.compile(r"<[^>]+>")


class BraveSearch:
    """The Brave Search API as a ``WebSearch`` port (module doc)."""

    name = "brave"

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 timeout_s: float = TIMEOUT_S) -> None:  # fmt: skip
        """Search with ``api_key`` against ``base_url``; the key is never logged."""
        if not api_key:
            raise WebError("search_key_missing", f"set {KEY_ENV} to search with brave")
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def search(self, query: str, *, limit: int) -> SearchResults:
        """At most ``limit`` hits for ``query``, ranked as the provider ranks them."""
        url = f"{self.base_url}/res/v1/web/search?" + urlencode({"q": query, "count": limit})
        request = urllib.request.Request(url, headers={
            "Accept": "application/json", "X-Subscription-Token": self._key,
            "User-Agent": "aksha-runtime"})  # fmt: skip
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = response.read(5 * 1024 * 1024)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                raise TransientStepError("transient", f"brave answered {e.code}") from e
            raise WebError(SEARCH_FAILED, f"brave answered {e.code}") from e
        except (TimeoutError, urllib.error.URLError) as e:
            raise TransientStepError("transient", f"brave did not answer: {e}") from e
        try:
            data: Any = json.loads(body)
        except ValueError as e:
            raise WebError(SEARCH_FAILED, "brave answered something other than JSON") from e
        return SearchResults(query, self.name, tuple(_hits(data, limit)))


def _hits(data: Any, limit: int) -> list[SearchHit]:
    results = (data.get("web") or {}).get("results") or [] if isinstance(data, dict) else []
    out: list[SearchHit] = []
    for item in results:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        snippet = " ".join(_TAG.sub("", str(item.get("description", ""))).split())
        date = item.get("page_age") or None
        out.append(SearchHit(len(out) + 1, str(item.get("title", "")), str(item["url"]),
                             snippet, str(date)[:10] if date else None))  # fmt: skip
        if len(out) >= limit:
            break
    return out
