"""``test_loader_refuses[fixture]``: each refusal of a value, one mutation of the toy agent each.

Folder-level refusals (a missing prompt, a source root inside the agent, an import past the
API) are in ``test_loader.py``; they need files.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.api import (
    Agent,
    ArtifactKind,
    CheckResult,
    ModelAlias,
    Ok,
    Read,
    RelationKind,
    StopPolicyOptIn,
    Task,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
    ToolUse,
    artifact_keys,
    from_field,
    source_keys,
)
from aksha.runtime.definitions.loader import LoadedExtension, load_extensions
from aksha.runtime.definitions.validate import validate_agent
from tests.fixtures.agents.toy.agent import AGENT
from tests.support.agents import FIXTURES

MODULE = "toy/agent.py"


def _ok(view: Any) -> CheckResult:
    return CheckResult(ok=True)


@pytest.fixture(scope="module")
def echo() -> tuple[LoadedExtension, ...]:
    return load_extensions(["echo"], [FIXTURES / "extensions"])


def _task(agent: Agent, task_id: str, **changes: Any) -> Agent:
    """``agent`` with the top-level Task ``task_id`` changed."""
    tasks = tuple(replace(t, **changes) if t.id == task_id else t for t in agent.tasks)
    return replace(agent, tasks=tasks)


def _child(agent: Agent, parent: str, child: str, **changes: Any) -> Agent:
    collect = next(t for t in agent.tasks if t.id == parent)
    kids = tuple(replace(c, **changes) if c.id == child else c for c in collect.children)
    return _task(agent, parent, children=kids)


MINTING = ToolDecl(
    ToolSpec(
        name="fetch",
        owner="toy",
        effect="read",
        description="d",
        schema={"type": "object"},
        mints_citation_spans=True,
    ),
    body=lambda ctx, args: ToolOutcome(Ok({})),
)

CASES: list[tuple[str, str | None, str, Callable[[Agent], Agent]]] = [
    ("api_mismatch", None, "requires_api", lambda a: replace(a, requires_api=">=3")),
    ("requires_api_invalid", None, "requires_api", lambda a: replace(a, requires_api="two")),
    ("name_invalid", None, "name", lambda a: replace(a, name="Toy Agent")),
    ("unknown_extension", None, "extensions", lambda a: replace(a, extensions=("echo", "nope"))),
    (
        "name_invalid",
        None,
        "artifacts",
        lambda a: replace(a, artifacts=(*a.artifacts, ArtifactKind("a.b", {}))),
    ),
    (
        "duplicate_name",
        None,
        "artifacts",
        lambda a: replace(a, artifacts=(*a.artifacts, ArtifactKind("line", {}))),
    ),
    (
        "schema_invalid",
        None,
        "artifacts",
        lambda a: replace(a, artifacts=(*a.artifacts, ArtifactKind("bad", {"type": 5}))),
    ),
    (
        "derived_not_callable",
        None,
        "artifacts",
        lambda a: replace(a, artifacts=(*a.artifacts, ArtifactKind("d", {}, derived="f"))),  # type: ignore[arg-type]
    ),
    (
        "relation_end_unknown",
        None,
        "relations",
        lambda a: replace(a, relations=(RelationKind("r", src=("nope",), dst=("line",)),)),
    ),
    (
        "relation_end_unknown",
        None,
        "relations",
        lambda a: replace(a, relations=(RelationKind("r", src=("line",), dst=("record:other",)),)),
    ),
    (
        "provenance_relation_forbidden",
        None,
        "relations",
        lambda a: replace(
            a, relations=(RelationKind("derived_from", src=("line",), dst=("line",)),)
        ),
    ),
    ("unknown_tool", None, "tools", lambda a: replace(a, tools=(*a.tools, ToolUse("nope")))),
    (
        "duplicate_tool",
        None,
        "tools",
        lambda a: replace(a, tools=(*a.tools, ToolUse("save_artifact"))),
    ),
    (
        "unknown_stop_policy",
        None,
        "stop_policies",
        lambda a: replace(a, stop_policies=(StopPolicyOptIn("custom", {}),)),  # type: ignore[arg-type]
    ),
    (
        "alias_fallback_invalid",
        None,
        "model_aliases",
        lambda a: replace(
            a,
            model_aliases=(
                ModelAlias("x", "generation", fallback=("classify.tone",)),
                *a.model_aliases,
            ),
        ),
    ),
    (
        "duplicate_name",
        None,
        "model_aliases",
        lambda a: replace(a, model_aliases=(*a.model_aliases, *a.model_aliases)),
    ),
    (
        "duplicate_name",
        None,
        "model_aliases",
        lambda a: replace(
            a, model_aliases=(*a.model_aliases, ModelAlias("classify.tone", "generation"))
        ),
    ),
    ("check_not_callable", None, "completion", lambda a: replace(a, completion=("x",))),  # type: ignore[arg-type]
    ("duplicate_task", None, "tasks", lambda a: replace(a, tasks=(*a.tasks, a.tasks[1]))),
    (
        "tool_not_in_agent",
        "summarise",
        "tools",
        lambda a: _task(a, "summarise", tools=("save_artifact", "finish2")),
    ),
    (
        "tool_widens_parent",
        "collect/write",
        "tools",
        lambda a: _child(a, "collect", "write", tools=("save_artifact", "ask")),
    ),
    (
        "write_kind_unknown",
        "summarise",
        "writes",
        lambda a: _task(a, "summarise", writes=("nope",)),
    ),
    (
        "read_kind_unknown",
        "summarise",
        "reads",
        lambda a: _task(a, "summarise", reads=("toy.nope",)),
    ),
    (
        "read_never_written",
        "summarise",
        "reads",
        lambda a: _task(
            replace(a, artifacts=(*a.artifacts, ArtifactKind("orphan", {}))),
            "summarise",
            reads=("toy.orphan",),
        ),
    ),
    (
        "read_not_declared",
        "summarise",
        "reads",
        lambda a: _task(a, "summarise", reads=("other.thing",)),
    ),
    (
        "from_field_unread",
        "summarise",
        "reads",
        lambda a: _task(
            a, "summarise", reads=(Read("toy.line", keys=from_field("toy.summary", "ids")),)
        ),
    ),
    ("per_not_key_source", "collect", "per", lambda a: _task(a, "collect", per=lambda v: ())),
    (
        "per_kind_unknown",
        "collect",
        "per",
        lambda a: _task(a, "collect", per=artifact_keys("toy.nope", "ids")),
    ),
    ("needs_unknown", "summarise", "needs", lambda a: _task(a, "summarise", needs=("nope",))),
    ("needs_cycle", "collect", "needs", lambda a: _task(a, "collect", needs=("summarise",))),
    ("leaf_without_check", "summarise", "done", lambda a: _task(a, "summarise", done=None)),
    ("check_not_callable", "summarise", "done", lambda a: _task(a, "summarise", done="x")),
    (
        "tree_too_deep",
        "collect/read/a/b/c",
        "children",
        lambda a: _child(
            a,
            "collect",
            "read",
            done=None,
            children=(
                Task(
                    "a",
                    tools=("echo.lookup",),
                    children=(
                        Task(
                            "b",
                            tools=("echo.lookup",),
                            children=(Task("c", tools=("echo.lookup",), done=_ok),),
                        ),
                    ),
                ),
            ),
        ),
    ),
    (
        "unknown_stop_policy",
        "summarise",
        "stop_policies",
        lambda a: _task(a, "summarise", stop_policies=(StopPolicyOptIn("custom", {}),)),  # type: ignore[arg-type]
    ),
    (
        "unknown_model_alias",
        "collect/write",
        "model_aliases",
        lambda a: _child(a, "collect", "write", model_aliases=("nope",)),
    ),
    ("concurrency_unsupported", None, "concurrency", lambda a: replace(a, concurrency=2)),
    (
        "keyed_child_unsupported",
        "collect/read",
        "per",
        lambda a: _child(a, "collect", "read", per=source_keys("notes")),
    ),
    (
        "minting_tool_not_extracting",
        "summarise",
        "tools",
        lambda a: _task(
            replace(a, tools=(*a.tools, MINTING)), "summarise", tools=("save_artifact", "fetch")
        ),
    ),
]


def test_the_conformance_agent_has_no_refusal(echo: tuple[LoadedExtension, ...]) -> None:
    assert validate_agent(AGENT, module=MODULE, extensions=echo) == []


@pytest.mark.parametrize(
    ("code", "task", "field", "mutate"),
    CASES,
    ids=[f"{c[0]}:{c[2]}:{i}" for i, c in enumerate(CASES)],
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


def test_an_own_tool_is_usable_by_its_local_name(echo: tuple[LoadedExtension, ...]) -> None:
    agent = _task(
        replace(AGENT, tools=(*AGENT.tools, MINTING)),
        "collect",
        tools=("echo.lookup", "save_artifact", "fetch"),
    )
    assert validate_agent(agent, module=MODULE, extensions=echo) == []


def test_the_api_refusal_comes_first(echo: tuple[LoadedExtension, ...], tmp_path: Path) -> None:
    broken = _task(replace(AGENT, requires_api=">=3"), "summarise", writes=("nope",))
    codes = [r.code for r in validate_agent(broken, module=MODULE, extensions=echo)]
    assert codes[0] == "api_mismatch" and "write_kind_unknown" in codes


def test_api_refusals_accept_the_current_version() -> None:
    from aksha.runtime.definitions.validate import api_refusals

    assert api_refusals("m", ">=1.0,<2") == []
    assert [r.code for r in api_refusals("m", "<1")] == ["api_mismatch"]


def test_name_refusals_flag_bad_and_repeated_names() -> None:
    from aksha.runtime.definitions.validate import name_refusals

    codes = [r.code for r in name_refusals("m", "artifacts", ["a", "B", "a", ""])]
    assert codes == ["name_invalid", "duplicate_name", "name_invalid"]
    assert name_refusals("m", "model_aliases", ["classify.tone"], local=False) == []


def test_cross_agent_refusals_name_the_unknown_kind() -> None:
    from aksha.runtime.definitions.validate import cross_agent_refusals

    reader = replace(AGENT, name="reader", reads=("toy.*",), extensions=(),
                     tasks=(Task("look", reads=("toy.gone",), done=_ok),))  # fmt: skip
    (refusal,) = cross_agent_refusals([(reader, "reader/agent.py")], ["toy.line"])
    assert (refusal.code, refusal.task) == ("read_kind_unknown", "look")
    assert cross_agent_refusals([(reader, "reader/agent.py")], ["toy.gone"]) == []
