from aksha.runtime.context.types import CONTEXT_POLICY_VERSION, ContextPolicy, SummaryPolicy


def test_context_policy_defaults_follow_the_adr() -> None:
    p = ContextPolicy()
    assert (p.result_cap_chars, p.context_limit_chars, p.masking, p.summary) == (
        8_000,
        100_000,
        False,
        None,
    )
    assert SummaryPolicy().alias == "generate.summary"
    assert CONTEXT_POLICY_VERSION == 1
