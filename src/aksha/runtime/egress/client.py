"""``PolicedHttpClient``: the ``HttpClient`` the runtime hands a tool body (ADR-P26 D2 to D4).

For each hop the client resolves the host, checks the URL and every address
(:func:`check_destination`), records an :class:`EgressDecision`, and only then sends to the
first checked address, so a name cannot be re-resolved to another address between the
check and the connection. A redirect (301, 302, 303, 307, 308 with a ``Location``) is a new
hop, checked the same way; after ``max_redirects`` hops the client refuses
``too_many_redirects``. A refusal raises :class:`EgressRefused`, which the tool-call path
turns into the error the model reads (``egress_refused``) and never retries.

``decisions`` holds every hop's decision in order; the agent loop records them on the
step's ``step.completed`` under ``value["egress"]`` (D4).

``RefusingHttpClient`` is the client of a tool with no egress policy, or of a runtime
built with the network off: it refuses every URL (``no_egress_policy``, ``network_off``)
and records each refusal the same way, so the default is deny (amendment A1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from aksha.runtime.egress.check import check_destination, check_url, normal_host
from aksha.runtime.egress.errors import EgressRefused
from aksha.runtime.egress.ports import HttpRequest, HttpResponse, Resolver, Transport
from aksha.runtime.egress.types import EgressPolicy

REDIRECTS = frozenset({301, 302, 303, 307, 308})
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
"""The default body cap per response: 5 MiB."""


@dataclass(frozen=True)
class EgressDecision:
    """One hop's check: the URL, its host, the addresses it resolved to and the verdict."""

    policy: str
    url: str
    host: str
    addresses: tuple[str, ...]
    allowed: bool
    reason: str | None = None

    def as_json(self) -> dict[str, Any]:
        """The decision as ``step.completed`` records it."""
        out: dict[str, Any] = {
            "policy": self.policy,
            "url": self.url,
            "host": self.host,
            "addresses": list(self.addresses),
            "allowed": self.allowed,
        }
        if self.reason is not None:
            out["reason"] = self.reason
        return out


NO_EGRESS_POLICY = "no_egress_policy"
NETWORK_OFF = "network_off"


class RefusingHttpClient:
    """An ``HttpClient`` that refuses every destination with one reason (module doc)."""

    def __init__(self, reason: str = NO_EGRESS_POLICY, policy: str = "") -> None:
        """Refuse every URL for ``reason``; ``policy`` names the tool's policy, if any."""
        self.reason = reason
        self.policy = policy
        self._decisions: list[EgressDecision] = []

    @property
    def decisions(self) -> tuple[EgressDecision, ...]:
        """Every refusal so far, in order."""
        return tuple(self._decisions)

    def get(self, url: str, *, timeout_s: float) -> HttpResponse:
        """Refuse ``url``."""
        host = normal_host(urlsplit(url).hostname or "")
        self._decisions.append(EgressDecision(self.policy, url, host, (), False, self.reason))
        raise EgressRefused(host, self.reason, url)


def _port(url: str) -> int:
    parts = urlsplit(url)
    try:
        explicit = parts.port
    except ValueError:
        explicit = None
    if explicit is not None:
        return explicit
    return 443 if parts.scheme.lower() == "https" else 80


class PolicedHttpClient:
    """An ``HttpClient`` bound to one egress policy (module doc)."""

    def __init__(
        self,
        policy: EgressPolicy,
        resolver: Resolver,
        transport: Transport,
        *,
        max_redirects: int = 5,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        """Check every hop against ``policy``; resolve and send through the given adapters."""
        self.policy = policy
        self._resolver = resolver
        self._transport = transport
        self._max_redirects = max_redirects
        self._max_bytes = max_bytes
        self._decisions: list[EgressDecision] = []

    @property
    def decisions(self) -> tuple[EgressDecision, ...]:
        """Every hop's decision so far, in order."""
        return tuple(self._decisions)

    def _checked(self, url: str) -> str:
        """The address to connect to for ``url``; records the decision, raises on refusal."""
        host = normal_host(urlsplit(url).hostname or "")
        resolved: tuple[str, ...] = ()
        reason = check_url(self.policy, url)  # refused before any resolution
        if reason is None:
            resolved = tuple(self._resolver.resolve(host, _port(url)))
            reason = check_destination(self.policy, url, resolved)
        self._decisions.append(
            EgressDecision(self.policy.name, url, host, resolved, reason is None, reason)
        )
        if reason is not None:
            raise EgressRefused(host, reason, url)
        return resolved[0]

    def get(self, url: str, *, timeout_s: float) -> HttpResponse:
        """GET ``url`` under the policy, following checked redirects (module doc)."""
        current = url
        for _hop in range(self._max_redirects + 1):
            address = self._checked(current)
            request = HttpRequest("GET", current)
            response = self._transport.send(
                request, address, timeout_s=timeout_s, max_bytes=self._max_bytes
            )
            location = response.headers.get("location")
            if response.status not in REDIRECTS or not location:
                return response
            current = urljoin(current, location)
        host = normal_host(urlsplit(current).hostname or "")
        self._decisions.append(
            EgressDecision(self.policy.name, current, host, (), False, "too_many_redirects")
        )
        raise EgressRefused(host, "too_many_redirects", current)
