"""``check_destination``: the pure egress check on a URL and its resolved addresses (ADR-P26 D3)."""

from __future__ import annotations

import pytest

from aksha.runtime.egress.check import check_destination, host_matches
from aksha.runtime.egress.types import EgressPolicy

PUBLIC = ("93.184.215.14",)
OPEN = EgressPolicy("open", allow_hosts=("*",))
DOCS = EgressPolicy("docs", allow_hosts=("docs.example.org", "*.files.example.org"))


def test_a_public_https_host_passes_an_open_policy() -> None:
    assert check_destination(OPEN, "https://example.org/a", PUBLIC) is None


def test_a_policy_with_no_allow_entries_refuses_every_host() -> None:
    # Deny by default (amendment A1): an empty allowlist reaches nothing; "*" opens it.
    shut = EgressPolicy("shut")
    assert check_destination(shut, "https://example.org/a", PUBLIC) == "host_not_allowed"


def test_a_star_entry_does_not_open_private_addresses() -> None:
    assert check_destination(OPEN, "https://example.org/", ("10.0.0.1",)) == "private_address"


def test_a_scheme_outside_the_policy_is_refused() -> None:
    assert check_destination(OPEN, "http://example.org/a", PUBLIC) == "scheme_not_allowed"
    assert check_destination(OPEN, "file:///etc/passwd", ()) == "scheme_not_allowed"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.9",
        "192.168.1.1",
        "169.254.169.254",  # a cloud metadata address
        "100.64.0.1",  # shared address space
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fd00:ec2::254",
        "::ffff:127.0.0.1",  # an IPv4-mapped loopback
    ],
)
def test_a_private_loopback_link_local_or_metadata_address_is_refused(address: str) -> None:
    assert check_destination(OPEN, "https://innocent.example/", (address,)) == "private_address"


def test_one_private_address_among_public_ones_refuses_the_whole_host() -> None:
    resolved = ("93.184.215.14", "127.0.0.1")
    assert check_destination(OPEN, "https://rebind.example/", resolved) == "private_address"


def test_allow_private_lets_a_private_address_through() -> None:
    lab = EgressPolicy("lab", schemes=frozenset({"http"}), allow_hosts=("localhost",),
                       allow_private=True)  # fmt: skip
    assert check_destination(lab, "http://localhost:8888/", ("127.0.0.1",)) is None


def test_a_cidr_in_allow_hosts_names_a_private_range() -> None:
    lab = EgressPolicy("lab", allow_hosts=("10.9.0.0/16",))
    assert check_destination(lab, "https://10.9.1.1/", ("10.9.1.1",)) is None
    assert check_destination(lab, "https://10.8.1.1/", ("10.8.1.1",)) == "host_not_allowed"


def test_the_allowlist_matches_exact_hosts_and_star_subdomains() -> None:
    assert check_destination(DOCS, "https://docs.example.org/x", PUBLIC) is None
    assert check_destination(DOCS, "https://a.files.example.org/x", PUBLIC) is None
    assert check_destination(DOCS, "https://files.example.org/x", PUBLIC) == "host_not_allowed"
    assert check_destination(DOCS, "https://evil.org/x", PUBLIC) == "host_not_allowed"
    assert check_destination(DOCS, "https://docs.example.org.evil.org/", PUBLIC) == (
        "host_not_allowed"
    )


def test_the_denylist_wins_over_an_open_policy() -> None:
    p = EgressPolicy("p", allow_hosts=("*",), deny_hosts=("*.tracker.example",))
    assert check_destination(p, "https://x.tracker.example/", PUBLIC) == "host_denied"


def test_a_cidr_in_deny_hosts_refuses_a_host_that_resolves_into_it() -> None:
    """A deny entry that is a range is checked against every resolved address."""
    p = EgressPolicy("p", allow_hosts=("*",), deny_hosts=("93.184.0.0/16",))
    assert check_destination(p, "https://cdn.example.org/", PUBLIC) == "host_denied"
    other = EgressPolicy("p", allow_hosts=("*",), deny_hosts=("8.8.8.0/24",))
    assert check_destination(other, "https://cdn.example.org/", PUBLIC) is None


def test_hosts_compare_without_case_or_a_trailing_dot() -> None:
    assert host_matches("Docs.Example.ORG.", "docs.example.org")
    assert check_destination(DOCS, "https://DOCS.example.org./x", PUBLIC) is None


def test_a_url_with_no_host_or_with_credentials_is_refused() -> None:
    assert check_destination(OPEN, "https:///path", PUBLIC) == "no_host"
    assert check_destination(OPEN, "https://user:pw@example.org/", PUBLIC) == "credentials_in_url"


def test_a_host_that_resolved_to_nothing_is_refused() -> None:
    assert check_destination(OPEN, "https://example.org/", ()) == "no_address"


def test_an_unparseable_address_is_refused_not_trusted() -> None:
    assert check_destination(OPEN, "https://example.org/", ("not-an-ip",)) == "bad_address"
