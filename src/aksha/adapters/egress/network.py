"""``SocketResolver`` and ``HttpTransport``: the real network under ``PolicedHttpClient``.

The transport connects to the address it is given, never to the host name, so the address
the runtime checked is the address it reaches (ADR-P26 D3). For HTTPS the TLS handshake
still names the URL's host (server name indication) and the certificate is verified
against it with the system's trust store. Bodies are read up to ``max_bytes``; a longer
body is cut and marked ``truncated``. Redirects are not followed here: the client
re-checks each one.
"""

from __future__ import annotations

import http.client
import socket
import ssl
from urllib.parse import urlsplit

from aksha.runtime.egress.ports import HttpRequest, HttpResponse

USER_AGENT = "aksha-runtime"


class SocketResolver:
    """Resolves with ``getaddrinfo``; every address, IPv4 and IPv6, de-duplicated in order."""

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        """Every address ``host`` resolves to; empty when it does not resolve."""
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return ()
        seen: dict[str, None] = {}
        for info in infos:
            seen.setdefault(str(info[4][0]), None)
        return tuple(seen)


class _PinnedHTTPS(http.client.HTTPSConnection):
    """An HTTPS connection to a fixed address that verifies the certificate for ``host``."""

    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(raw, server_hostname=self.host)


class _PinnedHTTP(http.client.HTTPConnection):
    """A plain HTTP connection to a fixed address; ``Host`` still names the URL's host."""

    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class HttpTransport:
    """Sends one request to one checked address (module doc)."""

    def send(
        self, request: HttpRequest, address: str, *, timeout_s: float, max_bytes: int
    ) -> HttpResponse:
        """Connect to ``address``, send ``request``, read at most ``max_bytes`` of the body."""
        parts = urlsplit(request.url)
        host = parts.hostname or ""
        https = parts.scheme.lower() == "https"
        port = parts.port or (443 if https else 80)
        conn: http.client.HTTPConnection = (
            _PinnedHTTPS(host, port, address, timeout_s)
            if https
            else _PinnedHTTP(host, port, address, timeout_s)
        )
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        headers = {"user-agent": USER_AGENT, **dict(request.headers)}
        try:
            conn.request(request.method, path, headers=headers)
            raw = conn.getresponse()
            body = raw.read(max_bytes + 1)
            got = {k.lower(): v for k, v in raw.getheaders()}
            return HttpResponse(raw.status, got, body[:max_bytes], request.url,
                                truncated=len(body) > max_bytes)  # fmt: skip
        finally:
            conn.close()
