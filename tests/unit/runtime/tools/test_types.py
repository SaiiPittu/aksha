from typing import get_args

from aksha.runtime.artifacts.types import SaveRequest
from aksha.runtime.outbox.types import EffectSpec
from aksha.runtime.tools.loader_added import LOADER_ADDED_TOOLS
from aksha.runtime.tools.types import (
    EFFECT_CLASSES,
    EffectClass,
    Err,
    Ok,
    RetryPolicy,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
    ToolUse,
)


def test_reserved_tools_are_finish_ask_and_save_artifact() -> None:
    assert frozenset({"finish", "ask", "save_artifact"}) == LOADER_ADDED_TOOLS


def test_effect_classes_are_the_six_adr_names() -> None:
    names = {"read", "store", "external", "exec", "model", "reserved"}
    assert set(get_args(EffectClass)) == names == EFFECT_CLASSES


def test_tool_use_names_a_tool_and_may_override_retries() -> None:
    use = ToolUse("echo.lookup", retries=1)
    assert (use.tool, use.retries, use.timeout_ms) == ("echo.lookup", 1, None)


def test_tool_decl_pairs_a_spec_with_its_body() -> None:
    spec = ToolSpec(name="t", owner="x", effect="read", description="d", schema={})
    decl = ToolDecl(spec, body=None)
    assert decl.spec.retry == RetryPolicy() and not decl.spec.mints_citation_spans


def test_tool_spec_carries_one_effect_spec_and_no_recovery_fields() -> None:
    effect = EffectSpec(system="forms", key_fields=("/form_id",), semantics="reconcile")
    spec = ToolSpec(
        name="write_form", owner="x", effect="external", description="d", schema={},
        external=effect,
    )  # fmt: skip
    assert spec.external is effect and spec.approval is None and spec.sandbox is None
    for gone in ("recovery", "receiver", "idempotency_key_fields", "needs_approval"):
        assert not hasattr(spec, gone)


def test_outcome_holds_a_result_and_save_requests() -> None:
    ok = ToolOutcome(Ok({"n": 1}), saves=(SaveRequest("line", None, {"line": "x"}),))
    err = ToolOutcome(Err("schema", "bad", paths=("/n",)))
    assert isinstance(ok.result, Ok) and ok.saves[0].kind == "line"
    assert isinstance(err.result, Err) and err.result.paths == ("/n",) and err.saves == ()


def test_tool_context_has_identity_and_no_store_or_model_handle() -> None:
    ctx = ToolContext(
        owner="o", agent="a", task_path="collect[n1]", key="n1", step_id="s", attempt=1
    )
    assert ctx.attempt == 1
    for handle in ("store", "model", "adapter"):
        assert not hasattr(ctx, handle)


def test_tool_call_is_a_name_arguments_and_position() -> None:
    from aksha.runtime.tools.types import ToolCall

    call = ToolCall("fetch", {"url": "u"})
    assert (call.name, dict(call.arguments), call.position) == ("fetch", {"url": "u"}, 0)
