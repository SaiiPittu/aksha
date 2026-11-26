"""Plain HTTP fetch goes through the fetch tool's egress-policed client, and only through it.

The resolver and the transport are fakes, so nothing leaves the machine: the client checks
each resolved address and each redirect under the runtime's web policy (``aksha.web``),
records every decision, and refuses a private address.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from aksha.adapters.web.http import HttpFetch
from aksha.app import fetched_document
from aksha.extensions.ingest.types import Caps
from aksha.runtime.egress.client import PolicedHttpClient, RefusingHttpClient
from aksha.runtime.egress.errors import EgressRefused
from aksha.runtime.egress.ports import HttpRequest, HttpResponse
from aksha.runtime.web.types import WEB_POLICY, WebError
from tests.support.ingest_fixtures import need, text_pdf


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


def test_a_binary_answer_goes_through_document_ingestion_when_wired() -> None:
    need("pypdf")
    doc = f"{PAGE}/datasheet.pdf"
    pdf = text_pdf(["Datasheet: 24 V PoE, EUR 189 per unit"])
    client, _ = _client({doc: HttpResponse(200, {"content-type": "application/pdf"}, pdf, doc)})
    page = HttpFetch(convert=fetched_document).fetch(doc, client)
    assert page.status == 200 and "EUR 189 per unit" in page.text
    assert "datasheet.pdf" in page.text.split("\n\n", 1)[0]  # ingestion's header names it
    huge = HttpResponse(200, {"content-type": "application/pdf"}, b"%PDF" + b"0" * 64, doc)
    client, _ = _client({doc: huge})
    with pytest.raises(WebError) as e:
        HttpFetch(convert=lambda u, b, t: fetched_document(u, b, t, caps=Caps(max_bytes=16))
                  ).fetch(doc, client)  # fmt: skip
    assert e.value.code == "fetch_refused"
