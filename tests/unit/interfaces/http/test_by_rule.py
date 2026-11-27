"""``by`` is set from the principal: who a command is recorded as, and the one exception."""

from __future__ import annotations

import pytest

from aksha.interfaces.http.auth import ByRule
from aksha.interfaces.wire import WireInvalid
from aksha.runtime.session import named as n

STOP = n.Stop(owner="t", run_id="R-0001")


def test_an_account_records_its_user_and_a_default_by_becomes_it() -> None:
    rule = ByRule("user:u-1", "member")
    assert rule.bind(STOP).by == "user:u-1"  # no by: the default role, ``person``
    assert rule.bind(n.Stop(owner="t", run_id="R-0001", by="user:u-1")).by == "user:u-1"
    assert ByRule("local").bind(STOP).by == "local"


@pytest.mark.parametrize("by", ["agent:patent", "user:u-2", "local", "admin", "ops"])
def test_a_by_naming_anyone_else_is_refused(by: str) -> None:
    with pytest.raises(WireInvalid) as e:
        ByRule("user:u-1", "owner").bind(n.Stop(owner="t", run_id="R-0001", by=by))
    assert e.value.code == "by_not_allowed"


def test_the_trusted_header_mode_keeps_the_bodys_by() -> None:
    kept = n.Stop(owner="t", run_id="R-0001", by="ops")
    assert ByRule(None).bind(kept) is kept


def _decide(by: str) -> n.DecideCorrection:
    return n.DecideCorrection(owner="t", correction_id="C-0001", approve=True, by=by)


@pytest.mark.parametrize(
    ("role", "by", "allowed"),
    [
        ("member", "person", True),
        ("member", "legal", False),
        ("admin", "legal", True),
        ("owner", "legal", True),
        ("viewer", "person", False),
        ("owner", "user:u-2", False),
        ("owner", "agent:patent", False),
        ("owner", "local", False),
    ],
)
def test_a_correction_is_decided_under_a_role_the_principals_role_maps_to(
    role: str, by: str, allowed: bool
) -> None:
    rule = ByRule("user:u-1", role)
    if allowed:
        assert rule.bind(_decide(by)).by == by  # the runtime checks the approver role
    else:
        with pytest.raises(WireInvalid) as e:
            rule.bind(_decide(by))
        assert e.value.code == "by_not_allowed"
