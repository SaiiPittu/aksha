"""The live search adapter, tested only against a stub HTTP server on this machine.

No real search API is called and no real key exists here: the stub answers what the
provider's API answers in shape, and checks the key arrives in its header.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest

from aksha.adapters.search.brave import BraveSearch
from aksha.runtime.execution.errors import TransientStepError
from aksha.runtime.web.types import WebError

KEY = "stub-key-for-tests"
BODY = {
    "web": {
        "results": [
            {"title": "Kelvra TS-32", "url": "https://kelvra.example/product",
             "description": "A <strong>thermal</strong> occupancy sensor.", "page_age":
             "2026-10-14T00:00:00"},
            {"title": "No URL", "description": "skipped"},
            {"title": "Veyra Halo", "url": "https://veyra.example/product", "description": ""},
        ]
    }
}  # fmt: skip


class Stub(BaseHTTPRequestHandler):
    status = 200
    seen: ClassVar[list[dict[str, Any]]] = []

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        Stub.seen.append({"path": parts.path, "query": parse_qs(parts.query),
                          "key": self.headers.get("X-Subscription-Token")})  # fmt: skip
        body = json.dumps(BODY).encode() if Stub.status == 200 else b"{}"
        self.send_response(Stub.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    Stub.status, Stub.seen = 200, []
    httpd = HTTPServer(("127.0.0.1", 0), Stub)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_search_sends_the_query_and_the_key_and_reads_ranked_hits(server: str) -> None:
    found = BraveSearch(KEY, server).search("occupancy sensor", limit=5)
    (asked,) = Stub.seen
    assert asked["path"] == "/res/v1/web/search" and asked["key"] == KEY
    assert asked["query"] == {"q": ["occupancy sensor"], "count": ["5"]}
    assert [(h.rank, h.url) for h in found.hits] == [
        (1, "https://kelvra.example/product"),
        (2, "https://veyra.example/product"),
    ]
    assert found.hits[0].snippet == "A thermal occupancy sensor." and found.hits[0].date == (
        "2026-10-14"
    )
    assert found.provider == "brave"


def test_a_rate_limit_or_a_server_error_is_transient(server: str) -> None:
    for status in (429, 503):
        Stub.status = status
        with pytest.raises(TransientStepError):
            BraveSearch(KEY, server).search("x y", limit=3)


def test_a_refused_key_is_a_search_failure_the_model_reads(server: str) -> None:
    Stub.status = 401
    with pytest.raises(WebError) as e:
        BraveSearch(KEY, server).search("x y", limit=3)
    assert e.value.code == "search_failed"


def test_no_key_is_refused_before_any_call() -> None:
    with pytest.raises(WebError) as e:
        BraveSearch("")
    assert e.value.code == "search_key_missing" and "AKSHA_BRAVE_API_KEY" in e.value.message
