from aksha.runtime.context.types import (
    CONTEXT_POLICY_VERSION,
    DATA_RULE,
    DEFAULT_POLICY,
    ContextPolicy,
    Rendered,
    SummaryPolicy,
    effective_policy,
)


def test_context_policy_defaults_follow_the_adr() -> None:
    p = ContextPolicy()
    assert (p.result_cap_chars, p.read_cap_chars, p.read_block_cap_chars) == (8_000, 12_000, 40_000)
    assert (p.context_limit_chars, p.high_water_chars, p.low_water_chars) == (
        100_000,
        60_000,
        45_000,
    )
    assert (p.keep_recent_pairs, p.summary) == (3, None)
    assert SummaryPolicy().alias == "generate.summary"
    assert SummaryPolicy().max_per_instance == 2
    assert CONTEXT_POLICY_VERSION == 1


def test_masking_is_on_by_default_because_compaction_is_core() -> None:
    assert ContextPolicy().masking is False  # the frozen API keeps the ADR's field default
    assert DEFAULT_POLICY.masking is True
    assert effective_policy(None) is DEFAULT_POLICY
    own = ContextPolicy(keep_recent_pairs=5)
    assert effective_policy(own) is own


def test_rendered_records_its_spans_sorted() -> None:
    r = Rendered("k/a@v1", "summary", 10, frozenset({"EX-0002", "EX-0001"}))
    assert r.as_json() == {
        "ref": "k/a@v1",
        "mode": "summary",
        "chars": 10,
        "spans": ["EX-0001", "EX-0002"],
    }


def test_the_data_rule_names_tools_and_approvals() -> None:
    assert "data" in DATA_RULE and "approvals" in DATA_RULE
