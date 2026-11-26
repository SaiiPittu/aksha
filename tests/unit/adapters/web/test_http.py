"""Plain HTTP fetch goes through the fetch tool's egress-policed client, and only through it.

The resolver and the transport are fakes, so nothing leaves the machine: the client checks
each resolved address and each redirect under the runtime's web policy (``aksha.web``),
records every decision, and refuses a private address.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from aksha.adapters.web.http import HttpFetch
from aksha.runtime.egress.client import PolicedHttpClient, RefusingHttpClient
from aksha.runtime.egress.errors import EgressRefused
from aksha.runtime.egress.ports import HttpRequest, HttpResponse
from aksha.runtime.web.types import WEB_POLICY, WebError


class Resolver:
    def __init__(self, table: Mapping[str, tuple[str, ...]]) -> None:
        self.table = table

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        return self.table.get(host, ())


class Transport:
    def __init__(self, answers: Mapping[str, HttpResponse]) -> None:
        self.answers = answers
        self.sent: list[tuple[str, str]] = []

    def send(
        self, request: HttpRequest, address: str, *, timeout_s: float, max_bytes: int
    ) -> HttpResponse:
        self.sent.append((request.url, address))
        return self.answers[request.url]


PAGE = "https://vendor.example/product"
DNS = Resolver({"vendor.example": ("93.184.215.14",), "inside.example": ("10.0.0.5",)})


def _client(answers: Mapping[str, HttpResponse]) -> tuple[PolicedHttpClient, Transport]:
    transport = Transport(answers)
    return PolicedHttpClient(WEB_POLICY, DNS, transport), transport


def test_a_page_is_fetched_through_the_policed_client_and_decoded() -> None:
    body = "<p>Prix: 249 €</p>".encode("latin-1", errors="replace")
    answer = HttpResponse(200, {"content-type": "text/html; charset=latin-1"}, body, PAGE)
    client, transport = _client({PAGE: answer})
    page = HttpFetch().fetch(PAGE, client)
    assert (page.status, page.final_url) == (200, PAGE) and "Prix" in page.text
    assert transport.sent == [(PAGE, "93.184.215.14")]
    (decision,) = client.decisions
    assert decision.allowed and decision.policy == "aksha.web"


def test_a_private_address_is_refused_by_the_web_policy() -> None:
    client, transport = _client({})
    with pytest.raises(EgressRefused):
        HttpFetch().fetch("https://inside.example/admin", client)
    assert transport.sent == []


def test_with_the_network_off_every_fetch_is_refused() -> None:
    with pytest.raises(EgressRefused):
        HttpFetch().fetch(PAGE, RefusingHttpClient("network_off", "aksha.web"))


def test_a_binary_answer_is_refused_as_unsupported_content() -> None:
    answer = HttpResponse(200, {"content-type": "application/pdf"}, b"%PDF-1.7", PAGE)
    client, _ = _client({PAGE: answer})
    with pytest.raises(WebError) as e:
        HttpFetch().fetch(PAGE, client)
    assert e.value.code == "unsupported_content"


def test_an_error_status_is_returned_for_the_web_handle_to_refuse() -> None:
    client, _ = _client({PAGE: HttpResponse(404, {"content-type": "text/html"}, b"", PAGE)})
    assert HttpFetch().fetch(PAGE, client).status == 404
