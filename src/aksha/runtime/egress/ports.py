"""The ports of the runtime's HTTP client (ADR-P26 D2): what a body calls, what it calls down to.

``HttpClient`` is what a tool body gets as ``ToolContext.http``; a body that opens its own
socket is outside the contract (the loader's source scan refuses network modules). The
client is built from a ``Resolver`` (a host to its addresses) and a ``Transport`` (one
request to one already-checked address), both adapters (``aksha.adapters.egress``), so the
check between them stays in the runtime and is tested offline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol


@dataclass(frozen=True)
class HttpRequest:
    """One request: a method, an absolute URL and headers (names lower-case)."""

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class HttpResponse:
    """One response: status, headers (names lower-case), the body (capped) and its URL."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str
    truncated: bool = False


class HttpClient(Protocol):
    """The client a tool body reaches the network through, bound to the tool's policy."""

    def get(self, url: str, *, timeout_s: float) -> HttpResponse:
        """GET ``url``, following checked redirects; raises ``EgressRefused``."""
        ...


class Resolver(Protocol):
    """Host name resolution; the client checks every address it returns."""

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        """Every address ``host`` resolves to (empty when it does not resolve)."""
        ...


class Transport(Protocol):
    """Sends one request to one address the client already checked; never resolves."""

    def send(
        self, request: HttpRequest, address: str, *, timeout_s: float, max_bytes: int
    ) -> HttpResponse:
        """Connect to ``address`` (TLS names the URL's host), send, read at most ``max_bytes``."""
        ...


@dataclass(frozen=True)
class Network:
    """The resolver and transport a policed client is built over (the composition root's)."""

    resolver: Resolver
    transport: Transport
