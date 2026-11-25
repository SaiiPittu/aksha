from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from aksha.runtime.api import (
    DeclaredInterrupt,
    Ok,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
)
from aksha.runtime.canonical import canonical_json
from aksha.runtime.definitions.errors import LoadError
from aksha.runtime.definitions.loader import LoadedExtension, load_extensions
from aksha.runtime.definitions.tree_checks import walk_tasks
from aksha.runtime.tools.registry import ToolRegistry, UnknownTool, with_overrides
from aksha.runtime.tools.types import Err, ToolUse
from tests.fixtures.agents.toy.agent import AGENT
from tests.support.agents import FIXTURES


def _body(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
    return ToolOutcome(Ok({}))


@pytest.fixture(scope="module")
def echo() -> tuple[LoadedExtension, ...]:
    return load_extensions(["echo"], [FIXTURES / "extensions"])


@pytest.fixture
def registry(echo: tuple[LoadedExtension, ...]) -> ToolRegistry:
    return ToolRegistry.build(AGENT, echo)


def _with_own(schema: Mapping[str, Any]) -> Any:
    spec = ToolSpec(name="own", owner="toy", effect="read", description="d", schema=schema)
    return replace(AGENT, tools=(*AGENT.tools, ToolDecl(spec, _body)))


def test_build_lists_the_agents_tools_and_the_three_implicit_ones(registry: ToolRegistry) -> None:
    assert registry.names() == ("ask", "echo.lookup", "finish", "save_artifact")
    assert registry.get("echo.lookup").spec.owner == "echo"
    assert registry.validate() == []


def test_get_refuses_an_unknown_tool(registry: ToolRegistry) -> None:
    with pytest.raises(UnknownTool) as err:
        registry.get("nope")
    assert err.value.code == "unknown_tool"


def test_offered_is_the_task_list_plus_implicit_tools(registry: ToolRegistry) -> None:
    collect, summarise = AGENT.tasks
    assert [v.name for v in registry.offered(collect)] == ["echo.lookup", "finish", "save_artifact"]
    assert [v.name for v in registry.offered(summarise)] == ["finish", "save_artifact"]
    asking = replace(summarise, interrupt=DeclaredInterrupt("input", "which note?"))
    assert [v.name for v in registry.offered(asking)] == ["ask", "finish", "save_artifact"]


def test_offered_narrowing_removes_tools_but_never_reserved_ones(registry: ToolRegistry) -> None:
    collect = AGENT.tasks[0]
    assert [v.name for v in registry.offered(collect, frozenset())] == ["finish"]
    narrowed = registry.offered(collect, frozenset({"echo.lookup"}))
    assert [v.name for v in narrowed] == ["echo.lookup", "finish"]
    saves = registry.offered(collect, frozenset({"save_artifact"}))
    assert [v.name for v in saves] == ["finish", "save_artifact"]


def test_is_reserved_means_the_reserved_effect_class(registry: ToolRegistry) -> None:
    assert registry.is_reserved("finish") and registry.is_reserved("ask")
    assert not registry.is_reserved("save_artifact")  # loader-added, but a store tool
    assert not registry.is_reserved("echo.lookup") and not registry.is_reserved("nope")


def test_published_schema_is_validated_schema(registry: ToolRegistry) -> None:
    for _, task in walk_tasks(AGENT.tasks):
        for view in registry.offered(task):
            validated = registry.validator(view.name).schema
            assert canonical_json(view.schema) == canonical_json(validated)
    err = registry.check_arguments("echo.lookup", {"text": 5})
    assert isinstance(err, Err) and err.paths == ("/text",)
    assert registry.check_arguments("echo.lookup", {"text": "hi"}) is None


def test_registry_is_frozen_against_later_edits_of_a_schema(
    echo: tuple[LoadedExtension, ...],
) -> None:
    schema: dict[str, Any] = {"type": "object", "properties": {"id": {"type": "string"}}}
    registry = ToolRegistry.build(_with_own(schema), echo)
    before = registry.canonical_hash()
    schema["properties"]["id"]["type"] = "integer"
    assert registry.canonical_hash() == before
    assert registry.check_arguments("own", {"id": "x"}) is None


def test_canonical_hash_is_stable_and_changes_with_one_schema(
    echo: tuple[LoadedExtension, ...],
) -> None:
    a = ToolRegistry.build(_with_own({"type": "object"}), echo).canonical_hash()
    again = ToolRegistry.build(_with_own({"type": "object"}), echo).canonical_hash()
    other = ToolRegistry.build(_with_own({"type": "object", "required": ["id"]}), echo)
    assert a == again and a.startswith("sha256:")
    assert other.canonical_hash() != a


def test_build_refuses_an_unsafe_registry(echo: tuple[LoadedExtension, ...]) -> None:
    spec = ToolSpec(name="own", owner="toy", effect="external", description="d",
                    schema={"type": "object"})  # fmt: skip
    agent = replace(AGENT, tools=(*AGENT.tools, ToolDecl(spec, _body)))
    with pytest.raises(LoadError) as err:
        ToolRegistry.build(agent, echo)
    assert [r.code for r in err.value.refusals] == ["external_missing"]


def test_check_arguments_of_an_unknown_tool_raises(registry: ToolRegistry) -> None:
    with pytest.raises(UnknownTool):
        registry.check_arguments("nope", {})


def test_view_shows_name_description_and_schema(registry: ToolRegistry) -> None:
    view = registry.view("echo.lookup")
    assert (view.name, view.description) == ("echo.lookup", "Echo the given text back.")
    assert view.schema["required"] == ("text",)


def test_canonical_is_plain_json_without_bodies(registry: ToolRegistry) -> None:
    rows = registry.canonical()
    assert [r["name"] for r in rows] == list(registry.names())
    lookup = next(r for r in rows if r["name"] == "echo.lookup")
    assert lookup["has_body"] is True and lookup["spec"]["effect"] == "read"
    assert lookup["spec"]["retry"]["retry_on"] == ["timeout", "transient"]
    canonical_json(rows)  # every value is JSON


def test_with_overrides_writes_the_tool_use_into_the_retry_policy(
    echo: tuple[LoadedExtension, ...],
) -> None:
    decl = echo[0].tools["echo.lookup"]
    assert with_overrides(decl, ToolUse("echo.lookup")) is decl
    tuned = with_overrides(decl, ToolUse("echo.lookup", retries=0, timeout_ms=750))
    assert (tuned.spec.retry.max_retries, tuned.spec.retry.timeout_ms) == (0, 750)
    assert tuned.body is decl.body and decl.spec.retry.max_retries == 2


def test_listed_holds_own_and_contributed_tools_only(registry: ToolRegistry) -> None:
    assert sorted(registry.listed()) == ["echo.lookup"]
    assert "finish" in registry.names()
