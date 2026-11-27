"""``Agent.reuse`` in the loader (ADR-P04 R1): ``on`` by default, ``off`` recorded on runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from aksha.runtime.definitions.errors import LoadError
from aksha.runtime.definitions.hashing import run_started_fields
from aksha.runtime.definitions.loader import load_agents
from tests.support.agents import Plugins, copy_plugins


@pytest.fixture
def plugins(tmp_path: Path) -> Plugins:
    return copy_plugins(tmp_path)


def _declare(plugins: Plugins, setting: str) -> None:
    plugins.edit(
        "agents/toy/agent.py",
        'extensions=("echo",),',
        f'extensions=("echo",),\n    reuse={setting!r},',
    )


def test_a_definition_that_declares_nothing_reuses_and_records_nothing(plugins: Plugins) -> None:
    (loaded,) = load_agents([plugins.toy], [plugins.extensions])
    assert loaded.agent.reuse == "on"
    assert "reuse" not in run_started_fields(loaded, runtime_commit="abc", dirty=False)


def test_off_is_recorded_on_run_started_and_changes_the_definition(plugins: Plugins) -> None:
    (before,) = load_agents([plugins.toy], [plugins.extensions])
    _declare(plugins, "off")
    (loaded,) = load_agents([plugins.toy], [plugins.extensions])
    assert loaded.agent.reuse == "off"
    assert run_started_fields(loaded, runtime_commit="abc", dirty=False)["reuse"] == "off"
    assert loaded.definition_hash != before.definition_hash


def test_an_unknown_setting_is_refused(plugins: Plugins) -> None:
    _declare(plugins, "sometimes")
    with pytest.raises(LoadError) as err:
        load_agents([plugins.toy], [plugins.extensions])
    (refusal,) = err.value.refusals
    assert (refusal.code, refusal.field) == ("reuse_unknown", "reuse")
