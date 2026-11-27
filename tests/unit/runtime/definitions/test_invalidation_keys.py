"""``Agent.invalidation_keys`` in the loader and on ``run.started`` (ADR-P05 amendment M2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from aksha.runtime.definitions.errors import LoadError
from aksha.runtime.definitions.hashing import definition_hash, run_started_fields
from aksha.runtime.definitions.loader import load_agents
from aksha.runtime.definitions.versions import API_VERSION
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.invalidation.confirm import after
from aksha.runtime.invalidation.keying import inherited_keys, run_keys
from aksha.runtime.projections.view import ArtifactsView, LogView
from tests.support.agents import Plugins, copy_plugins


@pytest.fixture
def plugins(tmp_path: Path) -> Plugins:
    return copy_plugins(tmp_path)


def _declare(plugins: Plugins, keys: str) -> None:
    plugins.edit(
        "agents/toy/agent.py",
        'extensions=("echo",),',
        f'extensions=("echo",),\n    invalidation_keys={keys!r},',
    )


def test_a_definition_that_declares_nothing_is_read_keyed(plugins: Plugins) -> None:
    (loaded,) = load_agents([plugins.toy], [plugins.extensions])
    assert loaded.agent.invalidation_keys == "reads"
    assert run_started_fields(loaded, runtime_commit="c", dirty=False)["invalidation_keys"] == (
        "reads"
    )


def test_citations_load_and_are_recorded_on_run_started(plugins: Plugins) -> None:
    _declare(plugins, "citations")
    (loaded,) = load_agents([plugins.toy], [plugins.extensions])
    fields = run_started_fields(loaded, runtime_commit="c", dirty=False)
    assert fields["invalidation_keys"] == "citations"


def test_an_unknown_keying_is_refused(plugins: Plugins) -> None:
    _declare(plugins, "spans")
    with pytest.raises(LoadError) as err:
        load_agents([plugins.toy], [plugins.extensions])
    (refusal,) = err.value.refusals
    assert (refusal.code, refusal.field) == ("invalidation_keys_unknown", "invalidation_keys")


def test_the_default_keeps_the_definition_hash_and_citations_change_it(plugins: Plugins) -> None:
    (loaded,) = load_agents([plugins.toy], [plugins.extensions])
    tools = loaded.registry.canonical_hash()

    def hashed(keys: str | None) -> str:
        return definition_hash(plugins.toy, loaded.extensions, API_VERSION, tools_hash=tools,
                               invalidation="off", invalidation_keys=keys)  # fmt: skip

    assert hashed(None) == hashed("reads")  # a definition from before M2 keeps its hash
    assert hashed("citations") != hashed("reads")


def test_the_research_and_patent_agents_key_on_citations() -> None:
    from aksha.agents.patent.agent import AGENT as PATENT
    from aksha.agents.research.agent import AGENT as RESEARCH

    assert (RESEARCH.invalidation_keys, PATENT.invalidation_keys) == ("citations", "citations")


def test_a_run_that_records_none_is_read_keyed_and_a_rebuild_inherits() -> None:
    def started(run: str, **extra: str) -> NewEvent:
        return NewEvent("run.started", {"agent": "a", "cause": "request", **extra}, run=run)

    empty = LogView("o1", 0, (), ArtifactsView((), 0))
    view = after(empty, [started("R-0001"), started("R-0002", invalidation_keys="citations"),
                         started("R-0003", invalidation_keys="citations")])  # fmt: skip
    keys = run_keys(view)
    assert (keys["R-0001"], keys["R-0002"]) == ("reads", "citations")
    assert inherited_keys(view, ["R-0002", "R-0003"]) == "citations"
    assert inherited_keys(view, ["R-0001", "R-0002"]) == "reads"  # any read-keyed run wins
