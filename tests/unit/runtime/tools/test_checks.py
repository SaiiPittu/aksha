"""``test_loader_refuses[fixture]`` for the tool registry: an unsafe registry never loads.

Each case is one mutation of the conformance agent, run through ``validate_agent`` (the
loader's value checks). The first eight groups are the eight refusals of the registry;
the rest are the build refusals and amendments S1 to S3 of ADR-P10.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.api import (
    Agent,
    ApprovalRule,
    ArtifactKind,
    EffectSpec,
    Ok,
    SandboxNeeds,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
    ToolUse,
)
from aksha.runtime.definitions.errors import LoadError
from aksha.runtime.definitions.loader import LoadedExtension, load_extensions
from aksha.runtime.definitions.validate import validate_agent
from aksha.runtime.tools.checks import spec_refusals
from tests.fixtures.agents.toy.agent import AGENT
from tests.support.agents import FIXTURES, copy_plugins

MODULE = "toy/agent.py"
OBJECT = {"type": "object", "properties": {"id": {"type": "string"}}}
TABLE = ArtifactKind("table", OBJECT, save="tool_only")


def _body(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    return ToolOutcome(Ok({}))


def _facts(view: Any, args: Mapping[str, object]) -> Mapping[str, str]:
    return {}


def _own(*, body: Any = _body, **changes: Any) -> Callable[[Agent], Agent]:
    """The agent with one own tool, ``own``, whose spec has ``changes``."""
    spec = ToolSpec(name="own", owner="toy", effect="read", description="d", schema=OBJECT)
    decl = ToolDecl(replace(spec, **changes), body=body)
    return lambda a: replace(a, tools=(*a.tools, decl))


def _task(task_id: str, **changes: Any) -> Callable[[Agent], Agent]:
    def mutate(agent: Agent) -> Agent:
        tasks = tuple(replace(t, **changes) if t.id == task_id else t for t in agent.tasks)
        return replace(agent, tasks=tasks)

    return mutate


def _then(*steps: Callable[[Agent], Agent]) -> Callable[[Agent], Agent]:
    def mutate(agent: Agent) -> Agent:
        for step in steps:
            agent = step(agent)
        return agent

    return mutate


SEND = EffectSpec(system="send", key_fields=("/id",), semantics="reconcile")

CASES: list[tuple[str, str, str | None, str, Callable[[Agent], Agent]]] = [
    # 1. A tool that is not registered, or is owned by another agent.
    ("unregistered_tool", "unknown_tool", None, "tools",
     lambda a: replace(a, tools=(*a.tools, ToolUse("nope")))),
    ("other_agents_tool", "tool_owner_invalid", None, "tools", _own(owner="other")),
    # 2. A child Task whose tools widen its parent's.
    ("child_widens_tools", "tool_widens_parent", "collect/read", "tools",
     _then(_own(), lambda a: replace(a, tasks=(
         replace(a.tasks[0], children=(
             replace(a.tasks[0].children[0], tools=("echo.lookup", "own")),
             a.tasks[0].children[1],
         )),
         a.tasks[1],
     )))),
    # 3. A citation-minting tool on a Task that does not extract.
    ("raw_read_without_extracts", "minting_tool_not_extracting", "summarise", "tools",
     _then(_own(mints_citation_spans=True), _task("summarise", tools=("save_artifact", "own")))),
    # 4. Collections and kinds outside the agent's rights.
    ("collection_outside_reads", "collection_not_read", None, "tools",
     _own(collections=frozenset({"secrets"}))),
    ("kind_outside_reads", "read_kind_not_granted", None, "tools",
     _own(reads_kinds=frozenset({"other.thing"}))),
    ("write_outside_own_kinds", "write_kind_not_owned", None, "tools",
     _own(effect="store", writes_kinds=frozenset({"other.thing"}))),
    # 5. An external tool with no effect spec (S1), and the effect spec's own refusals.
    ("external_without_recovery", "external_missing", None, "tools", _own(effect="external")),
    ("effect_spec_on_read", "external_not_allowed", None, "tools", _own(external=SEND)),
    ("reconcile_without_system", "reconcile_without_system", None, "tools",
     _own(effect="external", external=replace(SEND, system=None))),
    ("key_field_not_pointer", "key_field_invalid", None, "tools",
     _own(effect="external", external=replace(SEND, key_fields=("id",)))),
    # ADR-P29 B3: record identity is a JSON pointer inside the request identity.
    ("record_field_not_pointer", "record_field_invalid", None, "tools",
     _own(effect="external", external=replace(SEND, record_fields=("id",)))),
    ("record_field_outside_key", "record_field_not_keyed", None, "tools",
     _own(effect="external", external=replace(SEND, record_fields=("/kind",)))),
    ("semantics_unknown", "semantics_unknown", None, "tools",
     _own(effect="external", external=replace(SEND, semantics="twice"))),  # type: ignore[arg-type]
    ("approval_without_external", "approval_without_external", None, "tools",
     _own(approval=ApprovalRule(facts=_facts))),
    # 6. An exec tool with no sandbox needs.
    ("exec_without_sandbox", "exec_without_sandbox", None, "tools", _own(effect="exec")),
    ("sandbox_on_read", "sandbox_not_allowed", None, "tools", _own(sandbox=SandboxNeeds())),
    # 7. A model tool whose alias is missing or not declared.
    ("model_tool_without_alias", "model_alias_missing", None, "tools",
     _own(effect="model", body=None)),
    ("unknown_alias", "model_alias_unknown", None, "tools",
     _own(effect="model", body=None, model_alias="nope")),
    ("alias_on_read", "model_alias_not_allowed", None, "tools", _own(model_alias="classify.tone")),
    # 8. A tool-only kind that no listed tool writes.
    ("tool_only_kind_without_tool", "tool_only_kind_without_tool", None, "artifacts",
     lambda a: replace(a, artifacts=(*a.artifacts, TABLE))),
    # Build refusals.
    ("unknown_effect", "effect_unknown", None, "tools", _own(effect="magic")),
    ("schema_does_not_compile", "schema_invalid", None, "tools", _own(schema={"type": 5})),
    ("schema_not_an_object", "schema_not_object", None, "tools", _own(schema={"type": "string"})),
    ("reserved_by_an_agent", "reserved_effect_not_allowed", None, "tools",
     _own(effect="reserved")),
    ("reserved_by_an_agent_no_body", "reserved_effect_not_allowed", None, "tools",
     _own(effect="reserved", body=None)),
    ("body_on_model", "body_not_allowed", None, "tools",
     _own(effect="model", model_alias="classify.tone")),
    ("body_on_model", "body_not_allowed", None, "tools",
     _own(effect="model", model_alias="classify.tone")),
    ("read_without_body", "body_missing", None, "tools", _own(body=None)),
    # S2: result_key.
    ("result_key_not_pointer", "result_key_invalid", None, "tools",
     _own(recorded_query=True, result_key="id")),
    ("result_key_without_query", "result_key_without_query", None, "tools",
     _own(result_key="/id")),
    # S3: egress.
    ("egress_on_store", "egress_not_allowed", None, "tools",
     _own(effect="store", egress="echo.web")),
    ("egress_unknown", "egress_policy_unknown", None, "tools", _own(egress="echo.web")),
]  # fmt: skip


@pytest.fixture(scope="module")
def echo() -> tuple[LoadedExtension, ...]:
    return load_extensions(["echo"], [FIXTURES / "extensions"])


@pytest.mark.parametrize(
    ("code", "task", "field", "mutate"),
    [c[1:] for c in CASES],
    ids=[c[0] for c in CASES],
)
def test_loader_refuses(
    echo: tuple[LoadedExtension, ...],
    code: str,
    task: str | None,
    field: str,
    mutate: Callable[[Agent], Agent],
) -> None:
    refusals = validate_agent(mutate(AGENT), module=MODULE, extensions=echo)
    assert refusals, "expected a refusal"
    first = refusals[0]
    assert (first.code, first.task, first.field, first.module) == (code, task, field, MODULE)
    assert first.message


def test_exec_tool_without_sandbox_needs_refused(echo: tuple[LoadedExtension, ...]) -> None:
    codes = [r.code for r in validate_agent(_own(effect="exec")(AGENT), module=MODULE,
                                            extensions=echo)]  # fmt: skip
    assert codes == ["exec_without_sandbox"]
    ok = _own(effect="exec", sandbox=SandboxNeeds())(AGENT)
    assert validate_agent(ok, module=MODULE, extensions=echo) == []


def test_safe_effectful_tools_load(echo: tuple[LoadedExtension, ...]) -> None:
    """The worked examples of S1: a reconciled send with approval, a form write without."""
    send = _own(effect="external", external=SEND, approval=ApprovalRule(facts=_facts))
    form = _own(effect="external", external=replace(SEND, system=None, semantics="at_most_once"))
    query = _own(recorded_query=True, result_key="/id")
    table = _then(
        lambda a: replace(a, artifacts=(*a.artifacts, TABLE)),
        _own(effect="store", writes_kinds=frozenset({"table"})),
    )  # fmt: skip
    for mutate in (send, form, query, table):
        assert validate_agent(mutate(AGENT), module=MODULE, extensions=echo) == []


def test_spec_refusals_of_a_safe_spec_are_empty() -> None:
    spec = ToolSpec(name="own", owner="toy", effect="read", description="d", schema=OBJECT)
    assert spec_refusals("own", ToolDecl(spec, _body), declarer="toy", module=MODULE) == []


def test_an_extension_with_an_unsafe_tool_is_refused(tmp_path: Path) -> None:
    plugins = copy_plugins(tmp_path)
    plugins.edit("extensions/echo/__init__.py", "body=lookup", "body=None")
    with pytest.raises(LoadError) as err:
        load_extensions(["echo"], [plugins.extensions])
    assert [r.code for r in err.value.refusals] == ["body_missing"]


def test_declarer_of_names_the_extension_or_the_agent() -> None:
    from aksha.runtime.tools.checks import declarer_of

    assert declarer_of("echo.lookup", "toy") == "echo"
    assert declarer_of("own", "toy") == "toy"


def test_rights_of_reads_collections_kinds_aliases(echo: tuple[LoadedExtension, ...]) -> None:
    from aksha.runtime.tools.checks import rights_of

    rights = rights_of(AGENT, echo)
    assert rights.collections == {"notes"}
    assert rights.readable_kinds == rights.writable_kinds == {"toy.line", "toy.summary"}
    assert rights.aliases == {"classify.tone"} and rights.egress == frozenset()


def test_rights_refusals_list_every_ask_beyond_the_rights(
    echo: tuple[LoadedExtension, ...],
) -> None:
    from aksha.runtime.tools.checks import rights_of, rights_refusals

    spec = ToolSpec(name="own", owner="toy", effect="store", description="d", schema=OBJECT,
                    collections=frozenset({"notes", "secrets"}),
                    reads_kinds=frozenset({"line", "other.x"}),
                    writes_kinds=frozenset({"summary", "other.y"}))  # fmt: skip
    found = rights_refusals("own", ToolDecl(spec, _body), rights_of(AGENT, echo),
                            declarer="toy", module=MODULE)  # fmt: skip
    codes = [r.code for r in found]
    assert codes == ["collection_not_read", "read_kind_not_granted", "write_kind_not_owned"]


def test_tool_only_refusals_accept_a_kind_some_tool_writes() -> None:
    from aksha.runtime.tools.checks import tool_only_refusals

    agent = replace(AGENT, artifacts=(*AGENT.artifacts, TABLE))
    spec = ToolSpec(name="own", owner="toy", effect="store", description="d", schema=OBJECT,
                    writes_kinds=frozenset({"table"}))  # fmt: skip
    assert tool_only_refusals(agent, {"own": ToolDecl(spec, _body)}, module=MODULE) == []
    (refusal,) = tool_only_refusals(agent, {}, module=MODULE)
    assert (refusal.code, refusal.field) == ("tool_only_kind_without_tool", "artifacts")


def test_tool_refusals_check_spec_before_rights(echo: tuple[LoadedExtension, ...]) -> None:
    from aksha.runtime.tools.checks import tool_refusals

    spec = ToolSpec(name="own", owner="other", effect="read", description="d", schema=OBJECT,
                    collections=frozenset({"secrets"}))  # fmt: skip
    found = tool_refusals(AGENT, {"own": ToolDecl(spec, _body)}, echo, module=MODULE)
    assert [r.code for r in found] == ["tool_owner_invalid"]
    assert tool_refusals(AGENT, {}, echo, module=MODULE) == []
