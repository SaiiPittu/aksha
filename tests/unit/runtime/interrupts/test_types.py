from aksha.runtime.interrupts.types import DeclaredInterrupt


def test_declared_interrupt_is_input_or_choice() -> None:
    assert DeclaredInterrupt("input", "which notes?").kind == "input"


def test_approval_rule_holds_facts_and_optional_targets() -> None:
    from aksha.runtime.interrupts.types import ApprovalRule

    def facts(view: object, args: object) -> dict[str, str]:
        return {"recipient": "R-1"}

    rule = ApprovalRule(facts=facts)
    assert rule.targets is None
    with_targets = ApprovalRule(facts=facts, targets=lambda a: (str(a["document"]),))
    assert with_targets.targets is not None
    assert with_targets.targets({"document": "d@1"}) == ("d@1",)
