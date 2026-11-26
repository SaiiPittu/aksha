"""Plain HTTP fetch as a ``WebFetch`` port: one GET through the tool's policed client.

The adapter never opens a socket itself: it calls the ``HttpClient`` the attempt hands it,
the client bound to the fetch tool's egress policy (``aksha.web``), so every hop, redirects
included, is resolved, checked and recorded (ADR-P26 D2 to D4). With the network off
(``AKSHA_NETWORK`` unset) that client refuses every URL (``network_off``).

A text answer (HTML, JSON, XML, Markdown, CSV, plain text) is decoded with the charset its
``Content-Type`` names, UTF-8 when it names none, undecodable bytes replaced. A binary
answer (a PDF, an image) is refused ``unsupported_content``: this build parses text only.
"""

from __future__ import annotations

import re

from aksha.runtime.egress.ports import HttpClient
from aksha.runtime.web.types import FetchedPage, WebError

TIMEOUT_S = 30.0
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml",
              "application/ld+json", "application/rss+xml", "application/atom+xml")  # fmt: skip
_CHARSET = re.compile(r"charset=([\w.-]+)", re.I)


class HttpFetch:
    """Fetches a page over the attempt's policed HTTP client (module doc)."""

    name = "http"

    def __init__(self, timeout_s: float = TIMEOUT_S) -> None:
        """Fetch with a per-request timeout of ``timeout_s`` seconds."""
        self.timeout_s = timeout_s

    def fetch(self, url: str, http: HttpClient) -> FetchedPage:
        """GET ``url``; the body decoded as text, or ``unsupported_content`` for binary."""
        response = http.get(url, timeout_s=self.timeout_s)
        kind = response.headers.get("content-type", "")
        base = kind.split(";", 1)[0].strip().lower()
        if base and not base.startswith(TEXT_TYPES):
            raise WebError("unsupported_content", f"{url} is {base}, not text")
        found = _CHARSET.search(kind)
        try:
            text = response.body.decode(found.group(1) if found else "utf-8", errors="replace")
        except LookupError:
            text = response.body.decode("utf-8", errors="replace")
        return FetchedPage(url, response.url or url, response.status, kind, text,
                           response.truncated)  # fmt: skip
