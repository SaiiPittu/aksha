"""``PolicedHttpClient``: the client the runtime hands tool bodies (ADR-P26 D2 to D4).

The resolver and the transport are fakes, so nothing leaves the machine. The client checks
every resolved address, connects only to a checked one, re-checks every redirect and
records each decision.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from aksha.runtime.egress.client import PolicedHttpClient
from aksha.runtime.egress.errors import EgressRefused
from aksha.runtime.egress.ports import HttpRequest, HttpResponse
from aksha.runtime.egress.types import EgressPolicy


class FakeResolver:
    def __init__(self, table: Mapping[str, tuple[str, ...]]) -> None:
        self.table = table
        self.asked: list[str] = []

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        self.asked.append(host)
        return self.table.get(host, ())


class FakeTransport:
    def __init__(self, answers: Mapping[str, HttpResponse]) -> None:
        self.answers = answers
        self.sent: list[tuple[str, str]] = []

    def send(
        self, request: HttpRequest, address: str, *, timeout_s: float, max_bytes: int
    ) -> HttpResponse:
        self.sent.append((request.url, address))
        return self.answers[request.url]


def _ok(url: str, body: bytes = b"hello") -> HttpResponse:
    return HttpResponse(200, {"content-type": "text/plain"}, body, url)


def _redirect(url: str, to: str) -> HttpResponse:
    return HttpResponse(302, {"location": to}, b"", url)


POLICY = EgressPolicy("docs", allow_hosts=("docs.example.org", "cdn.example.org"))
DNS = FakeResolver(
    {
        "docs.example.org": ("93.184.215.14",),
        "cdn.example.org": ("93.184.215.15",),
        "rebind.example.org": ("127.0.0.1",),
    }
)


def test_an_allowed_get_connects_to_the_checked_address_and_records_it() -> None:
    transport = FakeTransport({"https://docs.example.org/a": _ok("https://docs.example.org/a")})
    client = PolicedHttpClient(POLICY, DNS, transport)
    response = client.get("https://docs.example.org/a", timeout_s=5)
    assert response.status == 200 and response.body == b"hello"
    assert transport.sent == [("https://docs.example.org/a", "93.184.215.14")]
    (decision,) = client.decisions
    assert decision.allowed and decision.host == "docs.example.org"
    assert decision.as_json()["addresses"] == ["93.184.215.14"]


def test_a_refused_host_raises_before_any_connection() -> None:
    transport = FakeTransport({})
    client = PolicedHttpClient(POLICY, DNS, transport)
    with pytest.raises(EgressRefused) as refused:
        client.get("https://evil.example/", timeout_s=5)
    assert refused.value.code == "egress_refused"
    assert (refused.value.host, refused.value.reason) == ("evil.example", "host_not_allowed")
    assert transport.sent == []
    assert [d.allowed for d in client.decisions] == [False]


def test_a_redirect_to_a_private_address_is_refused_at_the_hop() -> None:
    lax = EgressPolicy("lax", allow_hosts=("*",))
    first = "https://docs.example.org/a"
    transport = FakeTransport({first: _redirect(first, "https://rebind.example.org/meta")})
    client = PolicedHttpClient(lax, DNS, transport)
    with pytest.raises(EgressRefused) as refused:
        client.get(first, timeout_s=5)
    assert refused.value.reason == "private_address"
    assert [(d.host, d.allowed) for d in client.decisions] == [
        ("docs.example.org", True),
        ("rebind.example.org", False),
    ]
    assert len(transport.sent) == 1


def test_a_relative_redirect_is_resolved_against_the_hop_and_followed() -> None:
    first, second = "https://docs.example.org/a", "https://docs.example.org/b"
    transport = FakeTransport({first: _redirect(first, "/b"), second: _ok(second, b"moved")})
    client = PolicedHttpClient(POLICY, DNS, transport)
    response = client.get(first, timeout_s=5)
    assert response.body == b"moved" and response.url == second
    assert len(client.decisions) == 2


def test_a_redirect_loop_stops_at_the_cap() -> None:
    a, b = "https://docs.example.org/a", "https://cdn.example.org/b"
    transport = FakeTransport({a: _redirect(a, b), b: _redirect(b, a)})
    client = PolicedHttpClient(POLICY, DNS, transport, max_redirects=3)
    with pytest.raises(EgressRefused) as refused:
        client.get(a, timeout_s=5)
    assert refused.value.reason == "too_many_redirects"
    assert len(transport.sent) == 4


def test_the_body_cap_is_passed_to_the_transport() -> None:
    seen: list[int] = []

    class Capped(FakeTransport):
        def send(
            self, request: HttpRequest, address: str, *, timeout_s: float, max_bytes: int
        ) -> HttpResponse:
            seen.append(max_bytes)
            return _ok(request.url)

    PolicedHttpClient(POLICY, DNS, Capped({}), max_bytes=1234).get(
        "https://docs.example.org/a", timeout_s=1
    )
    assert seen == [1234]


def test_a_url_refused_before_resolution_never_reaches_the_resolver() -> None:
    dns = FakeResolver({})
    client = PolicedHttpClient(POLICY, dns, FakeTransport({}))
    with pytest.raises(EgressRefused):
        client.get("http://docs.example.org/", timeout_s=1)
    assert dns.asked == [] and client.decisions[0].reason == "scheme_not_allowed"


def test_a_cidr_entry_allows_a_literal_address_after_resolution() -> None:
    lab = EgressPolicy("lab", allow_hosts=("10.9.0.0/16",))
    url = "https://10.9.1.1/x"
    client = PolicedHttpClient(lab, FakeResolver({"10.9.1.1": ("10.9.1.1",)}),
                               FakeTransport({url: _ok(url)}))  # fmt: skip
    assert client.get(url, timeout_s=1).status == 200
