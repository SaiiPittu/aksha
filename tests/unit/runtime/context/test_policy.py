from dataclasses import replace
from pathlib import Path

import pytest

from aksha.runtime.context import policy as policy_module
from aksha.runtime.context.policy import policy_hash, policy_material, policy_refusals
from aksha.runtime.context.types import DEFAULT_POLICY, ContextPolicy, SummaryPolicy
from aksha.runtime.definitions.hashing import definition_hash

BASE = ContextPolicy(masking=True)


@pytest.mark.parametrize(
    "changed",
    [
        replace(BASE, result_cap_chars=4_000),
        replace(BASE, read_cap_chars=10_000),
        replace(BASE, read_block_cap_chars=30_000),
        replace(BASE, context_limit_chars=90_000),
        replace(BASE, masking=False),
        replace(BASE, high_water_chars=50_000),
        replace(BASE, low_water_chars=40_000),
        replace(BASE, keep_recent_pairs=4),
        replace(BASE, exempt_tools=frozenset({"recall"})),
        replace(BASE, summary=None),
        replace(BASE, summary=SummaryPolicy(max_per_instance=5)),
    ],
)
def test_every_policy_field_changes_its_hash(changed: ContextPolicy) -> None:
    assert policy_hash(changed) != policy_hash(BASE)


def test_no_policy_hashes_as_the_default() -> None:
    assert policy_hash(None) == policy_hash(DEFAULT_POLICY) == policy_hash(BASE)


def test_policy_version_is_in_the_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    before = policy_hash(BASE)
    monkeypatch.setattr(policy_module, "CONTEXT_POLICY_VERSION", 2)
    assert policy_material(BASE)["version"] == 2
    assert policy_hash(BASE) != before


def test_policy_change_refuses_resume(tmp_path: Path) -> None:
    (tmp_path / "agent.py").write_text("AGENT = None\n")
    old = definition_hash(
        tmp_path, [], "1.0", tools_hash="sha256:t", context_hash=policy_hash(BASE)
    )
    new = definition_hash(
        tmp_path, [], "1.0", tools_hash="sha256:t",
        context_hash=policy_hash(replace(BASE, keep_recent_pairs=5)),
    )  # fmt: skip
    assert old != new


def test_a_sound_policy_loads() -> None:
    assert policy_refusals(BASE, ["read_file"]) == []
    assert policy_refusals(replace(BASE, summary=SummaryPolicy()), []) == []


@pytest.mark.parametrize(
    ("policy", "says"),
    [
        (replace(BASE, low_water_chars=70_000), "low_water_chars"),
        (replace(BASE, high_water_chars=120_000), "high_water_chars"),
        (replace(BASE, read_block_cap_chars=100_000), "read_block_cap_chars"),
        (replace(BASE, keep_recent_pairs=0), "keep_recent_pairs"),
        (replace(BASE, exempt_tools=frozenset({"recall"})), "exempt_tools"),
        (replace(BASE, result_cap_chars=10), "result_cap_chars"),
        (replace(BASE, summary=SummaryPolicy(trigger_chars=200_000)), "trigger_chars"),
        (replace(BASE, summary=SummaryPolicy(keep_recent_pairs=0)), "summary.keep_recent_pairs"),
    ],
)
def test_the_loader_refuses_an_unsound_policy(policy: ContextPolicy, says: str) -> None:
    problems = policy_refusals(policy, ["read_file"])
    assert problems and any(says in p for p in problems)
