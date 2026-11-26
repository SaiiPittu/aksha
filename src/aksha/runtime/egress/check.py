"""``check_destination``: whether a policy lets a URL reach its resolved addresses (ADR-P26 D3).

Pure: the caller resolves the host and passes every address it got; the check never
touches the network. The order, first refusal wins:

1. The scheme is in ``policy.schemes`` (``scheme_not_allowed``), the URL has a host
   (``no_host``) and carries no user or password (``credentials_in_url``).
2. ``deny_hosts`` refuses a matching host (``host_denied``); a CIDR deny entry refuses a
   host any of whose resolved addresses lies in it, after resolution.
3. ``allow_hosts`` must match the host (``host_not_allowed``); an empty allowlist matches
   nothing, so a policy denies by default (ADR-P26 amendment A1). An entry is an exact host
   (``docs.example.org``), a star for any subdomain (``*.example.org``, which does not match
   ``example.org`` itself), a lone ``*`` for any host, or a CIDR range (``10.9.0.0/16``),
   which matches a host whose every resolved address lies in it. ``*`` opens names only:
   step 4 still refuses a private address.
4. The host resolved to at least one address (``no_address``), each one parses
   (``bad_address``), and every one is a global address unless the policy allows private
   ones or a CIDR entry names it (``private_address``). Private, loopback, link-local
   (which holds the cloud metadata address), shared, reserved and multicast ranges are
   all non-global; an IPv4-mapped IPv6 address is judged as its IPv4 address. One bad
   address refuses the host, so a name that resolves to a public and a private address
   cannot be steered to the private one.

Hosts compare without case and without a trailing dot.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from urllib.parse import urlsplit

from aksha.runtime.egress.types import EgressPolicy

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def normal_host(host: str) -> str:
    """``host`` lower-cased, without a trailing dot."""
    return host.strip().rstrip(".").lower()


def host_matches(host: str, entry: str) -> bool:
    """Whether ``host`` matches one allow or deny entry (exact, or ``*.`` for subdomains)."""
    h, e = normal_host(host), normal_host(entry)
    if e == "*":
        return bool(h)
    if e.startswith("*."):
        return h.endswith(e[1:]) and h != e[2:]
    return h == e


def _network(entry: str) -> IPNetwork | None:
    if "/" not in entry:
        return None
    try:
        return ipaddress.ip_network(entry, strict=False)
    except ValueError:
        return None


def _address(text: str) -> IPAddress | None:
    try:
        addr = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _in_any(addr: IPAddress, networks: Sequence[IPNetwork]) -> bool:
    return any(addr.version == n.version and addr in n for n in networks)


def check_url(policy: EgressPolicy, url: str) -> str | None:
    """Steps 1 to 3 that need no resolution, or None. A CIDR allow entry defers step 3."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in policy.schemes:
        return "scheme_not_allowed"
    if parts.username is not None or parts.password is not None:
        return "credentials_in_url"
    host = normal_host(parts.hostname or "")
    if not host:
        return "no_host"
    if any(host_matches(host, d) for d in policy.deny_hosts if _network(d) is None):
        return "host_denied"
    has_cidr = any(_network(e) is not None for e in policy.allow_hosts)
    if not has_cidr and not any(host_matches(host, e) for e in policy.allow_hosts):
        return "host_not_allowed"
    return None


def check_destination(policy: EgressPolicy, url: str, resolved: Sequence[str]) -> str | None:
    """The refusal reason for ``url`` resolving to ``resolved`` under ``policy``, or None."""
    reason = check_url(policy, url)
    if reason is not None:
        return reason
    host = normal_host(urlsplit(url).hostname or "")
    networks = [n for n in (_network(e) for e in policy.allow_hosts) if n is not None]
    names = [e for e in policy.allow_hosts if _network(e) is None]
    if not resolved:
        return "no_address"
    addresses: list[IPAddress] = []
    for text in resolved:
        addr = _address(text)
        if addr is None:
            return "bad_address"
        addresses.append(addr)
    denied = [n for n in (_network(e) for e in policy.deny_hosts) if n is not None]
    if any(_in_any(a, denied) for a in addresses):
        return "host_denied"
    by_cidr = bool(networks) and all(_in_any(a, networks) for a in addresses)
    if not by_cidr and not any(host_matches(host, e) for e in names):
        return "host_not_allowed"
    if policy.allow_private or by_cidr:
        return None
    if any(not a.is_global or a.is_multicast for a in addresses):
        return "private_address"
    return None
