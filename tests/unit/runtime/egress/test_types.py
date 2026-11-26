from aksha.runtime.egress.types import EgressPolicy


def test_egress_policy_defaults_to_https_and_no_private_ranges() -> None:
    p = EgressPolicy("docs")
    assert p.schemes == frozenset({"https"}) and not p.allow_private


def test_an_egress_policy_allows_no_host_by_default() -> None:
    assert EgressPolicy("docs").allow_hosts == () and "*" not in EgressPolicy("docs").allow_hosts
