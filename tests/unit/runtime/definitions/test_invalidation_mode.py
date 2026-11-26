"""``Agent.invalidation`` and ``ToolSpec.edits`` in the loader (decision 38, ADR-P20 M1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from aksha.runtime.definitions.errors import LoadError
from aksha.runtime.definitions.hashing import definition_hash, run_started_fields
from aksha.runtime.definitions.loader import load_agents
from aksha.runtime.definitions.versions import API_VERSION
from aksha.runtime.tools.checks import spec_refusals
from aksha.runtime.tools.types import EditSpec, Ok, ToolDecl, ToolOutcome, ToolSpec
from tests.support.agents import Plugins, copy_plugins


@pytest.fixture
def plugins(tmp_path: Path) -> Plugins:
    return copy_plugins(tmp_path)


def _declare(plugins: Plugins, mode: str) -> None:
    plugins.edit(
        "agents/toy/agent.py",
        'extensions=("echo",),',
        f'extensions=("echo",),\n    invalidation={mode!r},',
    )


def test_a_definition_that_declares_nothing_is_off(plugins: Plugins) -> None:
    (loaded,) = load_agents([plugins.toy], [plugins.extensions])
    assert loaded.agent.invalidation == "off"
    fields = run_started_fields(loaded, runtime_commit="abc", dirty=False)
    assert fields["invalidation"] == "off"


@pytest.mark.parametrize("mode", ["off", "edit_guard", "notify", "rebuild"])
def test_each_mode_loads_and_is_recorded_on_run_started(plugins: Plugins, mode: str) -> None:
    _declare(plugins, mode)
    (loaded,) = load_agents([plugins.toy], [plugins.extensions])
    assert loaded.agent.invalidation == mode
    assert run_started_fields(loaded, runtime_commit="abc", dirty=False)["invalidation"] == mode


def test_an_unknown_mode_is_refused(plugins: Plugins) -> None:
    _declare(plugins, "always")
    with pytest.raises(LoadError) as err:
        load_agents([plugins.toy], [plugins.extensions])
    (refusal,) = err.value.refusals
    assert (refusal.code, refusal.field) == ("invalidation_unknown", "invalidation")
    assert "edit_guard" in refusal.message


def test_the_mode_is_hashed_even_where_no_file_shows_it(plugins: Plugins) -> None:
    """The default lives in the runtime, so the hash names the resolved mode, not only files."""
    (loaded,) = load_agents([plugins.toy], [plugins.extensions])
    tools = loaded.registry.canonical_hash()
    off = definition_hash(plugins.toy, loaded.extensions, API_VERSION, tools_hash=tools,
                          invalidation="off")  # fmt: skip
    rebuild = definition_hash(plugins.toy, loaded.extensions, API_VERSION, tools_hash=tools,
                              invalidation="rebuild")  # fmt: skip
    assert off != rebuild
    _declare(plugins, "rebuild")
    assert load_agents([plugins.toy], [plugins.extensions])[0].definition_hash != (
        loaded.definition_hash
    )


def test_instruction_files_are_refreshed_whatever_the_mode(plugins: Plugins) -> None:
    """An ``off`` agent still gets its changed instructions: they are part of its definition."""
    (before,) = load_agents([plugins.toy], [plugins.extensions])
    assert before.agent.invalidation == "off"
    instructions = plugins.toy / before.agent.instructions
    instructions.write_text(instructions.read_text(encoding="utf-8") + "\nBe brief.\n")
    (after,) = load_agents([plugins.toy], [plugins.extensions])
    assert after.definition_hash != before.definition_hash  # a run never resumes on the old text


def test_the_research_and_patent_agents_declare_rebuild() -> None:
    from aksha.agents.patent.agent import AGENT as PATENT
    from aksha.agents.research.agent import AGENT as RESEARCH

    assert (RESEARCH.invalidation, PATENT.invalidation) == ("rebuild", "rebuild")


# --- ToolSpec.edits ------------------------------------------------------------------------

ARGS = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "n": {"type": "integer"}},
    "required": ["path", "n"],
}


def _codes(spec: ToolSpec) -> list[str]:
    decl = ToolDecl(spec, None if spec.effect == "external" else _body)
    return [r.code for r in spec_refusals(spec.name, decl, declarer="coder", module="m")]


def _body(ctx: object, args: object) -> ToolOutcome:
    return ToolOutcome(Ok({}))


def test_an_edit_spec_on_a_store_tool_naming_a_required_string_loads() -> None:
    spec = ToolSpec("write", "coder", "store", "Write.", ARGS, edits=EditSpec("files", "/path"))
    assert _codes(spec) == []


@pytest.mark.parametrize(
    ("effect", "edit", "code"),
    [
        ("read", EditSpec("files", "/path"), "edits_not_allowed"),
        ("store", EditSpec("files", "/n"), "edits_invalid"),
        ("store", EditSpec("files", "/missing"), "edits_invalid"),
        ("store", EditSpec("files", "path"), "edits_invalid"),
        ("store", EditSpec("", "/path"), "edits_invalid"),
    ],
)
def test_a_bad_edit_spec_is_refused(effect: str, edit: EditSpec, code: str) -> None:
    spec = ToolSpec("write", "coder", effect, "Write.", ARGS, edits=edit)  # type: ignore[arg-type]
    assert code in _codes(spec)


def test_a_tool_that_edits_nothing_keeps_its_registry_hash() -> None:
    """``edits`` enters the tool registry's hash only when declared, so older hashes hold."""
    from aksha.runtime.tools.registry import _plain

    plain = ToolSpec("w", "coder", "store", "Write.", ARGS)
    assert _plain(plain)["edits"] is None  # the generic form has it
    from aksha.runtime.definitions.types import Agent
    from aksha.runtime.tools.registry import ToolRegistry

    def registry(spec: ToolSpec) -> ToolRegistry:
        agent = Agent("coder", "p", "i.md", (), (), tools=(ToolDecl(spec, _body),))
        return ToolRegistry(agent, {spec.name: ToolDecl(spec, _body)}, ())

    (entry,) = [e for e in registry(plain).canonical() if e["name"] == "w"]
    assert "edits" not in entry["spec"]
    edited = ToolSpec("w", "coder", "store", "Write.", ARGS, edits=EditSpec("files", "/path"))
    assert registry(edited).canonical_hash() != registry(plain).canonical_hash()
