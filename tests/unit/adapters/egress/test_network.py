"""``HttpTransport`` and ``SocketResolver`` against a loopback server (nothing leaves the host)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aksha.adapters.egress.network import HttpTransport, SocketResolver
from aksha.runtime.egress.client import PolicedHttpClient
from aksha.runtime.egress.errors import EgressRefused
from aksha.runtime.egress.ports import HttpRequest
from aksha.runtime.egress.types import EgressPolicy


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/hop":
            self.send_response(302)
            self.send_header("Location", "/end")
            self.end_headers()
            return
        body = f"host={self.headers['Host']} path={self.path}".encode() * 10
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[int]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_transport_connects_to_the_given_address_and_caps_the_body(server: int) -> None:
    url = f"http://app.test:{server}/x"
    got = HttpTransport().send(HttpRequest("GET", url), "127.0.0.1", timeout_s=5, max_bytes=20)
    assert got.status == 200 and got.truncated and len(got.body) == 20
    assert got.body.startswith(f"host=app.test:{server}".encode())


def test_the_resolver_answers_loopback_and_nothing_for_an_unknown_name() -> None:
    assert "127.0.0.1" in SocketResolver().resolve("localhost", 80) or (
        "::1" in SocketResolver().resolve("localhost", 80)
    )
    assert SocketResolver().resolve("no-such-host.invalid", 80) == ()


def test_the_policed_client_refuses_loopback_by_default(server: int) -> None:
    client = PolicedHttpClient(
        EgressPolicy("web", schemes=frozenset({"http"}), allow_hosts=("*",)),
        SocketResolver(),
        HttpTransport(),
    )
    with pytest.raises(EgressRefused) as refused:
        client.get(f"http://localhost:{server}/x", timeout_s=5)
    assert refused.value.reason == "private_address"


def test_a_policy_that_allows_private_follows_a_redirect_on_loopback(server: int) -> None:
    lab = EgressPolicy("lab", schemes=frozenset({"http"}), allow_hosts=("127.0.0.1",),
                       allow_private=True)  # fmt: skip
    client = PolicedHttpClient(lab, SocketResolver(), HttpTransport())
    got = client.get(f"http://127.0.0.1:{server}/hop", timeout_s=5)
    assert got.status == 200 and b"path=/end" in got.body
    assert [d.allowed for d in client.decisions] == [True, True]
