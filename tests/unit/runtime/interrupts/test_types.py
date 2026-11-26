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


def test_interrupt_view_scope_comes_from_the_envelope() -> None:
    from aksha.runtime.interrupts.types import InterruptView

    def view(run: str | None, instance: str | None, session: str | None) -> InterruptView:
        return InterruptView("I-1", "input", run, instance, session, {}, 1)

    assert view("R", "x", None).scope == "instance"
    assert view("R", None, None).scope == "run"
    assert view(None, None, "S").scope == "session"
    assert view("R", None, None).is_open


def test_action_body_round_trips_and_parses_its_arguments() -> None:
    from aksha.runtime.interrupts.types import ActionBody

    body = ActionBody("send", "o", '{"a":1}', (("k@v1", "sha256:1"),), "send:k", "h", {"f": "1"})
    assert ActionBody.from_json(body.as_json()) == body
    assert body.args() == {"a": 1} and body.target_refs() == ("k@v1",)
    bad = ActionBody("send", "o", "[1]", (), "send:k", "h", {})
    import pytest

    with pytest.raises(ValueError):
        bad.args()
