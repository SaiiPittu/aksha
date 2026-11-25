from __future__ import annotations

from pathlib import Path

import pytest

from aksha.runtime.api import API_VERSION
from aksha.runtime.definitions.errors import LoadError
from aksha.runtime.definitions.hashing import run_started_fields
from aksha.runtime.definitions.loader import discover, load_agents, load_extensions
from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue
from tests.support.agents import Plugins, copy_plugins


@pytest.fixture
def plugins(tmp_path: Path) -> Plugins:
    return copy_plugins(tmp_path)


def _refusal_codes(err: pytest.ExceptionInfo[LoadError]) -> list[str]:
    return [r.code for r in err.value.refusals]


def test_discover_lists_extensions_then_agents_in_sorted_order(plugins: Plugins) -> None:
    (plugins.agents / "alpha").mkdir()
    (plugins.agents / "alpha" / "agent.py").write_text("")
    (plugins.agents / "not_an_agent").mkdir()
    found = discover([plugins.agents], [plugins.extensions])
    assert found == (plugins.echo, plugins.agents / "alpha", plugins.toy)


def test_loader_round_trips_the_conformance_agent(plugins: Plugins) -> None:
    (loaded,) = load_agents([plugins.toy], [plugins.extensions])
    assert loaded.agent.name == "toy" and loaded.module == "toy/agent.py"
    assert sorted(loaded.kinds) == ["toy.line", "toy.summary"]
    assert sorted(loaded.relations) == ["toy.summarises"]
    assert sorted(loaded.tools) == ["echo.lookup"]
    assert sorted(loaded.projections) == ["echo.runs_started"]
    assert sorted(loaded.model_aliases) == ["toy.classify.tone"]
    assert [e.ext.name for e in loaded.extensions] == ["echo"]
    assert loaded.definition_hash.startswith("sha256:")
    collect = loaded.agent.tasks[0]
    assert [c.id for c in collect.children] == ["read", "write"]


def test_load_extensions_namespaces_contributions(plugins: Plugins) -> None:
    (echo,) = load_extensions(["echo"], [plugins.extensions])
    assert echo.ext.version == "0.1.0" and echo.folder == plugins.echo
    assert [p for p, _ in echo.file_hashes] == ["__init__.py"]
    assert sorted(echo.tools) == ["echo.lookup"]


def test_unknown_extension_is_refused(plugins: Plugins) -> None:
    with pytest.raises(LoadError) as err:
        load_extensions(["missing"], [plugins.extensions])
    assert _refusal_codes(err) == ["extension_not_found"]


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ('"""No value."""\n', "no_extension_value"),
        ("raise RuntimeError('boom')\n", "extension_import_failed"),
        (
            "from aksha.runtime.api import Extension\n"
            "EXTENSION = Extension(name='other', version='1', requires_api='>=1.0,<2')\n",
            "extension_name_mismatch",
        ),
        (
            "from aksha.runtime.api import Extension\n"
            "EXTENSION = Extension(name='echo', version='1', requires_api='>=9')\n",
            "api_mismatch",
        ),
    ],
)
def test_broken_extension_is_refused(plugins: Plugins, source: str, code: str) -> None:
    (plugins.echo / "__init__.py").write_text(source)
    with pytest.raises(LoadError) as err:
        load_extensions(["echo"], [plugins.extensions])
    assert _refusal_codes(err)[0] == code


def test_requires_api_mismatch_refused(plugins: Plugins) -> None:
    plugins.edit(
        "agents/toy/agent.py",
        'extensions=("echo",),',
        'extensions=("echo",),\n    requires_api=">=3",',
    )
    with pytest.raises(LoadError) as err:
        load_agents([plugins.toy], [plugins.extensions])
    first = err.value.refusals[0]
    assert (first.code, first.module, first.field) == (
        "api_mismatch",
        "toy/agent.py",
        "requires_api",
    )


@pytest.mark.parametrize(
    ("rel", "old", "new", "code", "field"),
    [
        ("agents/toy/agent.py", "AGENT = Agent(", "OTHER = Agent(", "no_agent_value", "AGENT"),
        ("agents/toy/agent.py", 'name="toy"', 'name="other"', "agent_name_mismatch", "name"),
        (
            "agents/toy/agent.py",
            "from . import checks",
            "from . import nothing_here",
            "agent_import_failed",
            "agent.py",
        ),
        (
            "agents/toy/agent.py",
            '"prompts/summarise.md"',
            '"prompts/gone.md"',
            "prompt_missing",
            "brief",
        ),
        (
            "agents/toy/agent.py",
            '"prompts/instructions.md"',
            '"../outside.md"',
            "prompt_missing",
            "instructions",
        ),
        (
            "agents/toy/checks.py",
            "from aksha.runtime.api import CheckResult",
            "from aksha.runtime.tasks.types import CheckResult",
            "import_outside_api",
            "imports",
        ),
    ],
)
def test_broken_agent_folder_is_refused(
    plugins: Plugins, rel: str, old: str, new: str, code: str, field: str
) -> None:
    plugins.edit(rel, old, new)
    with pytest.raises(LoadError) as err:
        load_agents([plugins.toy], [plugins.extensions])
    first = err.value.refusals[0]
    assert (first.code, first.field) == (code, field)


def test_source_root_inside_agent_is_refused(plugins: Plugins) -> None:
    plugins.edit(
        "agents/toy/agent.py",
        'extensions=("echo",),',
        'extensions=("echo",),\n    sources=(VersionedSourceSpec("notes", root="data/notes"),),',
    )
    plugins.edit("agents/toy/agent.py", "    Budget,", "    Budget,\n    VersionedSourceSpec,")
    with pytest.raises(LoadError) as err:
        load_agents([plugins.toy], [plugins.extensions])
    assert _refusal_codes(err) == ["source_root_inside_agent"]


def test_source_root_outside_agent_is_accepted(plugins: Plugins, tmp_path: Path) -> None:
    world = tmp_path / "world"
    plugins.edit(
        "agents/toy/agent.py",
        'extensions=("echo",),',
        f'extensions=("echo",),\n    sources=(VersionedSourceSpec("notes", root={str(world)!r}),),',
    )
    plugins.edit("agents/toy/agent.py", "    Budget,", "    Budget,\n    VersionedSourceSpec,")
    (loaded,) = load_agents([plugins.toy], [plugins.extensions])
    assert loaded.agent.sources[0].name == "notes"


def test_definition_hash_covers_prompts_and_extensions(plugins: Plugins) -> None:
    def current() -> str:
        return load_agents([plugins.toy], [plugins.extensions])[0].definition_hash

    base = current()
    assert current() == base  # stable across loads, bytecode caches included
    plugins.edit("agents/toy/prompts/summarise.md", "every", "each")
    after_prompt = current()
    assert after_prompt != base
    plugins.edit("extensions/echo/__init__.py", "Echo the", "Echo  the")
    assert current() != after_prompt


def test_tests_folder_is_outside_the_hash(plugins: Plugins) -> None:
    base = load_agents([plugins.toy], [plugins.extensions])[0].definition_hash
    (plugins.toy / "tests").mkdir()
    (plugins.toy / "tests" / "test_toy.py").write_text("def test_x() -> None: ...\n")
    assert load_agents([plugins.toy], [plugins.extensions])[0].definition_hash == base


def test_two_agents_with_one_name_are_refused(plugins: Plugins, tmp_path: Path) -> None:
    import shutil

    shutil.copytree(plugins.toy, tmp_path / "more" / "toy")
    with pytest.raises(LoadError) as err:
        load_agents([plugins.toy, tmp_path / "more" / "toy"], [plugins.extensions])
    assert _refusal_codes(err) == ["duplicate_name"]


def _second_agent(plugins: Plugins, reads: str, rights: str) -> Path:
    folder = plugins.agents / "reader"
    folder.mkdir()
    (folder / "__init__.py").write_text('"""A second agent."""\n')
    (folder / "prompts").mkdir()
    (folder / "prompts" / "i.md").write_text("Read.\n")
    (folder / "agent.py").write_text(
        "from aksha.runtime.api import Agent, CheckResult, Task\n"
        "def done(view):\n    return CheckResult(ok=True)\n"
        f"AGENT = Agent(name='reader', purpose='p', instructions='prompts/i.md', reads={rights},\n"
        f"    artifacts=(), tasks=(Task('look', reads=({reads!r},), done=done),))\n"
    )
    return folder


def test_cross_agent_read_of_a_declared_kind_is_accepted(plugins: Plugins) -> None:
    reader = _second_agent(plugins, "toy.line", "('toy.*',)")
    loaded = load_agents([reader, plugins.toy], [plugins.extensions])
    assert [d.agent.name for d in loaded] == ["reader", "toy"]


def test_cross_agent_read_of_an_unknown_kind_is_refused(plugins: Plugins) -> None:
    reader = _second_agent(plugins, "toy.nothing", "('toy.*',)")
    with pytest.raises(LoadError) as err:
        load_agents([reader, plugins.toy], [plugins.extensions])
    first = err.value.refusals[0]
    assert (first.code, first.module, first.task) == (
        "read_kind_unknown",
        "reader/agent.py",
        "look",
    )


def test_run_started_fields_record_the_definition(plugins: Plugins) -> None:
    (loaded,) = load_agents([plugins.toy], [plugins.extensions])
    fields = run_started_fields(loaded, runtime_commit="abc123", dirty=True)
    assert fields["definition_hash"] == loaded.definition_hash
    assert fields["api_version"] == API_VERSION
    assert fields["runtime_commit"] == "abc123" and fields["dirty"] is True
    echo = fields["extension_versions"]["echo"]
    assert echo["version"] == "0.1.0" and echo["hash"].startswith("sha256:")


# --- The boundary: an agent or extension imports aksha.runtime.api only (D1) -----------

LEAKS = [
    "from aksha import runtime\n",
    "import aksha.adapters.storage.sqlite\n",
    "from aksha.app import open_event_store\n",
    "import importlib\nimportlib.import_module('aksha.app')\n",
    "from aksha.extensions import other\n",
]


@pytest.mark.parametrize("source", LEAKS)
def test_agent_import_outside_the_api_is_refused(plugins: Plugins, source: str) -> None:
    (plugins.toy / "leak.py").write_text(source)
    with pytest.raises(LoadError) as err:
        load_agents([plugins.toy], [plugins.extensions])
    first = err.value.refusals[0]
    assert (first.code, first.module, first.field) == (
        "import_outside_api",
        "toy/leak.py",
        "imports",
    )


@pytest.mark.parametrize("source", LEAKS)
def test_extension_import_outside_the_api_is_refused(plugins: Plugins, source: str) -> None:
    (plugins.echo / "leak.py").write_text(source)
    with pytest.raises(LoadError) as err:
        load_extensions(["echo"], [plugins.extensions])
    assert _refusal_codes(err)[0] == "import_outside_api"


def test_agent_may_import_an_extension_it_names(plugins: Plugins) -> None:
    (plugins.toy / "uses.py").write_text("from aksha.extensions.echo import EXTENSION\n")
    (loaded,) = load_agents([plugins.toy], [plugins.extensions])
    assert loaded.agent.name == "toy"
    (plugins.echo / "uses.py").write_text("from aksha.extensions.echo import EXTENSION\n")
    with pytest.raises(LoadError) as err:  # an extension names no other extension
        load_extensions(["echo"], [plugins.extensions])
    assert _refusal_codes(err) == ["import_outside_api"]


# --- Names: one space for agents and extensions (D6) ----------------------------------


def test_agent_named_like_an_extension_it_loads_is_refused(plugins: Plugins) -> None:
    folder = plugins.agents / "echo"
    folder.mkdir()
    (folder / "prompts").mkdir()
    (folder / "prompts" / "i.md").write_text("Echo.\n")
    (folder / "agent.py").write_text(
        "from aksha.runtime.api import Agent\n"
        "AGENT = Agent(name='echo', purpose='p', instructions='prompts/i.md', reads=(),\n"
        "    artifacts=(), extensions=('echo',))\n"
    )
    with pytest.raises(LoadError) as err:
        load_agents([folder], [plugins.extensions])
    assert "namespace_collision" in _refusal_codes(err)


def test_a_full_name_declared_twice_is_refused(plugins: Plugins) -> None:
    plugins.edit(
        "extensions/echo/__init__.py",
        "    projections=(",
        '    artifacts=(ArtifactKind("line", {"type": "object"}),),\n    projections=(',
    )
    plugins.edit(
        "extensions/echo/__init__.py", "    ArtifactsView,", "    ArtifactKind,\n    ArtifactsView,"
    )
    folder = plugins.agents / "echo"
    folder.mkdir()
    (folder / "prompts").mkdir()
    (folder / "prompts" / "i.md").write_text("Echo.\n")
    (folder / "agent.py").write_text(
        "from aksha.runtime.api import Agent, ArtifactKind\n"
        "AGENT = Agent(name='echo', purpose='p', instructions='prompts/i.md', reads=(),\n"
        "    artifacts=(ArtifactKind('line', {'type': 'object'}),), extensions=('echo',))\n"
    )
    with pytest.raises(LoadError) as err:
        load_agents([folder], [plugins.extensions])
    refusal = next(r for r in err.value.refusals if r.code == "duplicate_name")
    assert refusal.field == "artifacts" and "echo.line" in refusal.message


# --- Tools an extension may not declare (P10 D8, P11 D3) -------------------------------


def test_extension_reserved_tool_is_refused(plugins: Plugins) -> None:
    plugins.edit("extensions/echo/__init__.py", 'effect="read",', 'effect="reserved",')
    plugins.edit("extensions/echo/__init__.py", "body=lookup,", "body=None,")
    with pytest.raises(LoadError) as err:
        load_extensions(["echo"], [plugins.extensions])
    assert _refusal_codes(err) == ["reserved_effect_not_allowed"]


# --- Extension event types reach the catalogue (P01 D4) --------------------------------


def test_extension_event_types_are_registered_after_a_clean_load(plugins: Plugins) -> None:
    catalogue = Catalogue(CORE_EVENT_TYPES)
    (loaded,) = load_agents([plugins.toy], [plugins.extensions], catalogue=catalogue)
    assert [s.name for s in loaded.extensions[0].event_types] == ["echo.looked_up"]
    assert catalogue.is_extension("echo.looked_up")
    load_agents([plugins.toy], [plugins.extensions], catalogue=catalogue)  # again: no error


@pytest.mark.parametrize(
    ("old", "new"),
    [('name="echo.looked_up"', 'name="other.thing"'), ('writer="echo"', 'writer="execution"'),
     ('name="echo.looked_up"', 'name="run.looked_up"')],
)  # fmt: skip
def test_extension_event_type_outside_its_namespace_is_refused(
    plugins: Plugins, old: str, new: str
) -> None:
    plugins.edit("extensions/echo/__init__.py", old, new)
    catalogue = Catalogue(CORE_EVENT_TYPES)
    with pytest.raises(LoadError) as err:
        load_agents([plugins.toy], [plugins.extensions], catalogue=catalogue)
    assert _refusal_codes(err) == ["event_type_invalid"]
    assert catalogue.names() == Catalogue(CORE_EVENT_TYPES).names()  # nothing registered


def test_a_refused_load_registers_no_event_type(plugins: Plugins) -> None:
    plugins.edit("agents/toy/agent.py", '"prompts/summarise.md"', '"prompts/gone.md"')
    catalogue = Catalogue(CORE_EVENT_TYPES)
    with pytest.raises(LoadError):
        load_agents([plugins.toy], [plugins.extensions], catalogue=catalogue)
    assert "echo.looked_up" not in catalogue


# --- ToolUse overrides are applied, or refused (P10) -----------------------------------


def test_tool_use_overrides_enter_the_registry_and_the_hash(plugins: Plugins) -> None:
    (base,) = load_agents([plugins.toy], [plugins.extensions])
    plugins.edit(
        "agents/toy/agent.py",
        'tools=(ToolUse("echo.lookup"),',
        'tools=(ToolUse("echo.lookup", retries=0, timeout_ms=500),',
    )
    (tuned,) = load_agents([plugins.toy], [plugins.extensions])
    retry = tuned.registry.get("echo.lookup").spec.retry
    assert (retry.max_retries, retry.timeout_ms) == (0, 500)
    assert tuned.tools["echo.lookup"].spec.retry == retry
    assert base.registry.get("echo.lookup").spec.retry.max_retries == 2
    assert tuned.definition_hash != base.definition_hash


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [('ToolUse("save_artifact")', 'ToolUse("save_artifact", retries=1)', "override_not_allowed"),
     ('ToolUse("echo.lookup")', 'ToolUse("echo.lookup", retries=-1)', "override_invalid"),
     ('ToolUse("echo.lookup")', 'ToolUse("echo.lookup", timeout_ms=0)', "override_invalid")],
)  # fmt: skip
def test_bad_tool_use_overrides_are_refused(
    plugins: Plugins, old: str, new: str, code: str
) -> None:
    plugins.edit("agents/toy/agent.py", old, new)
    with pytest.raises(LoadError) as err:
        load_agents([plugins.toy], [plugins.extensions])
    assert _refusal_codes(err) == [code]
