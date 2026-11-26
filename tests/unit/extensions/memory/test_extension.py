"""``MemoryExtension.apply`` and the spec (ADR-P09 D1, D3, D10)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import aksha.extensions as extensions_pkg
from aksha.extensions.memory import EXTENSION, MemoryRefused, MemorySpec, memory_extension
from aksha.extensions.memory.extension import not_recalled
from aksha.runtime.api import (
    Agent,
    ArtifactKind,
    CheckResult,
    ContextPolicy,
    Task,
    TaskView,
    ToolUse,
    source_keys,
)
from aksha.runtime.definitions.loader import LoadedExtension, load_agents, load_extensions
from aksha.runtime.definitions.validate import validate_agent
from tests.support.agents import copy_plugins

EXTENSIONS = Path(extensions_pkg.__file__).parent
OBJECT = {"type": "object"}
SPEC = MemorySpec(subjects=("research.geography",), intake_tasks=("scope",))


def _done(view: TaskView) -> CheckResult:
    return CheckResult(True)


def scout(**changes: Any) -> Agent:
    agent = Agent(
        name="scout",
        purpose="Scope a vendor search.",
        instructions="prompts/instructions.md",
        reads=(),
        artifacts=(ArtifactKind("scope", OBJECT), ArtifactKind("report", OBJECT)),
        tools=(ToolUse("save_artifact"),),
        tasks=(
            Task("scope", tools=("save_artifact",), writes=("scope",), done=_done),
            Task("report", needs=("scope",), tools=("save_artifact",), writes=("report",),
                 done=_done),
        ),
    )  # fmt: skip
    return replace(agent, **changes)


@pytest.fixture(scope="module")
def memory() -> tuple[LoadedExtension, ...]:
    return load_extensions(["memory"], [EXTENSIONS])


def test_the_extension_contributes_the_three_kinds_and_loads(
    memory: tuple[LoadedExtension, ...],
) -> None:
    (loaded,) = memory
    assert sorted(loaded.kinds) == ["memory.fact", "memory.preference", "memory.procedure"]
    assert EXTENSION.tools == () and EXTENSION.name == "memory"


def test_apply_puts_recall_first_in_the_intake_task_and_nowhere_else(
    memory: tuple[LoadedExtension, ...],
) -> None:
    applied = memory_extension(SPEC, "scout").apply(scout())
    scope, report = applied.tasks
    assert scope.tools == ("save_artifact", "recall", "remember", "forget")
    assert scope.writes == ("scope", "memory.preference", "memory.fact")
    forced = scope.stop_policies[0]
    assert forced.name == "forced_tool" and forced.params["tool"] == "recall"
    assert forced.params["when"] is not_recalled
    assert report == scout().tasks[1]
    assert applied.extensions == ("memory",)
    assert applied.context_policy is not None and "recall" in applied.context_policy.exempt_tools
    assert [getattr(t, "tool", None) or t.spec.name for t in applied.tools] == [  # type: ignore[union-attr]
        "save_artifact", "recall", "remember", "forget",
    ]  # fmt: skip
    assert validate_agent(applied, module="scout/agent.py", extensions=memory) == []


def test_apply_keeps_the_agents_own_context_policy(memory: tuple[LoadedExtension, ...]) -> None:
    own = scout(context_policy=ContextPolicy(result_cap_chars=2_000))
    applied = memory_extension(SPEC, "scout").apply(own)
    assert applied.context_policy == ContextPolicy(
        result_cap_chars=2_000, exempt_tools=frozenset({"recall"})
    )


def test_without_memory_on_an_extension_kind_in_writes_is_refused(
    memory: tuple[LoadedExtension, ...],
) -> None:
    """The loader allows a contributed kind in writes only when the agent names its extension."""
    agent = scout(
        tasks=(replace(scout().tasks[0], writes=("scope", "memory.fact")), scout().tasks[1])
    )
    codes = [r.code for r in validate_agent(agent, module="scout/agent.py", extensions=())]
    assert "write_kind_unknown" in codes
    named = replace(agent, extensions=("memory",))
    assert validate_agent(named, module="scout/agent.py", extensions=memory) == []


@pytest.mark.parametrize(
    ("spec", "agent", "code"),
    [
        (SPEC, scout(name="other"), "agent_mismatch"),
        (replace(SPEC, intake_tasks=("nope",)), scout(), "intake_unknown"),
        (replace(SPEC, intake_tasks=("report",)),
         scout(tasks=(Task("report", per=source_keys("notes"), done=_done),)), "intake_fan_out"),
        (SPEC, scout(tasks=(*scout().tasks[:1],
                            replace(scout().tasks[1], writes=("report", "memory.fact")))),
         "memory_write_outside_intake"),
        (SPEC, scout(tools=(ToolUse("save_artifact"), ToolUse("recall"))), "tool_name_taken"),
        (SPEC, scout(tasks=(*scout().tasks[:1],
                            replace(scout().tasks[1], tools=("save_artifact", "recall")))),
         "memory_tool_outside_intake"),
    ],
)  # fmt: skip
def test_apply_refuses(spec: MemorySpec, agent: Agent, code: str) -> None:
    with pytest.raises(MemoryRefused) as raised:
        memory_extension(spec, "scout").apply(agent)
    assert raised.value.code == code


@pytest.mark.parametrize(
    "changes",
    [
        {"subjects": ()},
        {"subjects": ("has space",)},
        {"write": frozenset({"memory.other"})},
        {"intake_tasks": ()},
        {"recall_limit": 0},
        {"text_cap_chars": 0},
    ],
)
def test_a_spec_apply_could_not_honour_is_refused(changes: dict[str, Any]) -> None:
    base: dict[str, Any] = {"subjects": ("research.geography",), "intake_tasks": ("scope",)}
    with pytest.raises(MemoryRefused) as raised:
        MemorySpec(**{**base, **changes})
    assert raised.value.code == "spec_invalid"
    assert raised.value.code == "spec_invalid"


def test_an_agent_without_a_memory_spec_hashes_the_same_with_memory_installed(
    tmp_path: Path,
) -> None:
    """D10: the hash covers only the extensions an agent names, so installing memory is free."""
    plugins = copy_plugins(tmp_path)
    (without,) = load_agents([plugins.toy], [plugins.extensions])
    (with_memory,) = load_agents([plugins.toy], [plugins.extensions, EXTENSIONS])
    assert without.definition_hash == with_memory.definition_hash
    assert [e.ext.name for e in with_memory.extensions] == ["echo"]


def test_the_runtime_core_never_imports_memory() -> None:
    """D1: a static scan; the dependency guard checks the same rule for every extension."""
    import ast

    runtime = EXTENSIONS.parent / "runtime"
    for path in sorted(runtime.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            assert not any(n.startswith("aksha.extensions") for n in names), path


def test_the_policy_gate_reads_no_artifact_so_memory_cannot_change_a_permission() -> None:
    """D7: the gate's input has no log view and no artifact, so no record can reach it."""
    from aksha.runtime.policy.types import GateInput

    assert not set(GateInput.__dataclass_fields__) & {"view", "log", "artifacts", "store"}
